#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.software_infrastructure.dedicated_parser_baseline import (  # noqa: E402
    open_read_only_database,
)
from technology.software_infrastructure.software_metric_expansion_release import (  # noqa: E402
    RELEASE_ID,
    approval_timestamp,
    build_cumulative_policy,
    build_expansion_decisions,
    policy_csv_rows,
    promote_proposal_rows,
    validate_policy_sources,
)
from technology.software_infrastructure.software_metric_review import (  # noqa: E402
    load_csv_rows,
    load_source_evidence,
    validate_review_rows,
)
from technology.software_infrastructure.software_parser_hydration import (  # noqa: E402
    atomic_csv,
    atomic_json,
)
from technology.software_infrastructure.software_specialized_metrics import (  # noqa: E402
    load_policy,
    validate_policy_payload,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOFTWARE_ROOT = PACKAGE_ROOT / "software_infrastructure"
GOLDEN_ROOT = PROJECT_ROOT / "dedicated_parser" / "golden_corpus"
DEFAULT_QUEUE = SOFTWARE_ROOT / "data" / "software_metrics_v1_expansion_queue.csv"
DEFAULT_PROPOSAL = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "dedicated_parser_governance"
    / "software_metrics_v3"
    / "software_metrics_v3_proposed_adjudication_workbook.csv"
)
DEFAULT_OFFICIAL_REVIEW = (
    SOFTWARE_ROOT
    / "review_policies"
    / "software_metrics_v3_adjudication_workbook.csv"
)
DEFAULT_APPROVAL = (
    SOFTWARE_ROOT / "review_policies" / "software_metrics_v3_approval.json"
)
DEFAULT_BASE_POLICY = GOLDEN_ROOT / "software_metrics_policy_v1.json"
DEFAULT_POLICY = GOLDEN_ROOT / "software_metrics_policy_v3.json"
DEFAULT_CORPUS = GOLDEN_ROOT / "software_metrics_v3_expansion_corpus.json"
DEFAULT_POLICY_CSV = (
    SOFTWARE_ROOT / "review_policies" / "software_metrics_v3_policy.csv"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "technology_reports"
    / "software_infrastructure"
    / "dedicated_parser_governance"
    / "software_metrics_v3"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Promote a hash-approved software metric proposal into the "
            "official review and a cumulative, hash-chained v3 policy."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--proposal", type=Path, default=DEFAULT_PROPOSAL)
    parser.add_argument(
        "--official-review", type=Path, default=DEFAULT_OFFICIAL_REVIEW
    )
    parser.add_argument("--approval-file", type=Path, default=DEFAULT_APPROVAL)
    parser.add_argument("--base-policy", type=Path, default=DEFAULT_BASE_POLICY)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--policy-csv", type=Path, default=DEFAULT_POLICY_CSV)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--approved-workbook-sha256", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reviewed-at-utc", default="")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the official review and sealed v3 release artifacts.",
    )
    return parser.parse_args()


def _csv_sha256(rows: list[dict[str, Any]]) -> str:
    if not rows:
        raise ValueError("Cannot hash an empty CSV")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(rows[0]),
        extrasaction="raise",
    )
    writer.writeheader()
    writer.writerows(rows)
    return hashlib.sha256(buffer.getvalue().encode("utf-8")).hexdigest()


def _artifact(path: Path) -> dict[str, Any]:
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
    try:
        int(expected_hash, 16)
    except ValueError as exc:
        raise ValueError(
            "approved-workbook-sha256 must contain 64 hex digits"
        ) from exc
    reviewer = args.reviewer.strip()
    if not reviewer:
        raise ValueError("reviewer is required")
    reviewed_at = args.reviewed_at_utc.strip() or approval_timestamp()

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
    queue_path = args.queue.expanduser().resolve()
    proposal_path = args.proposal.expanduser().resolve()
    official_path = args.official_review.expanduser().resolve()
    approval_path = args.approval_file.expanduser().resolve()
    base_policy_path = args.base_policy.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()
    corpus_path = args.corpus.expanduser().resolve()
    policy_csv_path = args.policy_csv.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    registry_path = (
        SOFTWARE_ROOT
        / "data"
        / "software_infrastructure_specialized_metric_registry.yaml"
    ).resolve()
    adapter_path = (SOFTWARE_ROOT / "dedicated_parser_adapter.py").resolve()

    actual_hash = file_sha256(proposal_path)
    if actual_hash != expected_hash:
        raise ValueError(
            "Approved workbook hash mismatch: "
            f"expected={expected_hash}, actual={actual_hash}"
        )
    queue_rows = load_csv_rows(queue_path)
    proposal_rows = load_csv_rows(proposal_path)
    official_rows = load_csv_rows(official_path)
    approved_rows = promote_proposal_rows(
        proposal_rows=proposal_rows,
        official_rows=official_rows,
        reviewer=reviewer,
        reviewed_at_utc=reviewed_at,
    )
    official_sha256 = _csv_sha256(approved_rows)
    timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0))
    base_policy = load_policy(base_policy_path)
    with open_read_only_database(db_path, timeout_sec=timeout_sec) as conn:
        expansion_source = load_source_evidence(
            conn,
            (str(row["source_evidence_key"]) for row in queue_rows),
        )
        review_errors, review_summary = validate_review_rows(
            approved_rows,
            queue_rows=queue_rows,
            source_evidence=expansion_source,
        )
        if review_errors:
            raise ValueError(
                "Approved review validation failed: "
                + "; ".join(review_errors[:10])
            )
        expansion_decisions = build_expansion_decisions(
            approved_rows=approved_rows,
            source_evidence=expansion_source,
            first_sequence=len(base_policy["decisions"]) + 1,
            previous_decision_hash=str(base_policy["chain_root_sha256"]),
            approved_workbook_sha256=expected_hash,
        )
        policy = build_cumulative_policy(
            base_policy=base_policy,
            expansion_decisions=expansion_decisions,
            approved_workbook_path=proposal_path,
            approved_workbook_sha256=expected_hash,
            official_review_path=official_path,
            official_review_sha256=official_sha256,
            registry_path=registry_path,
            adapter_path=adapter_path,
            reviewer=reviewer,
            reviewed_at_utc=reviewed_at,
        )
        validate_policy_payload(policy, source=str(policy_path))
        all_source = load_source_evidence(
            conn,
            (
                str(row["source_evidence_key"])
                for row in policy["decisions"]
            ),
        )
        source_errors = validate_policy_sources(
            policy,
            source_evidence=all_source,
        )
    if source_errors:
        raise ValueError(
            "Policy source validation failed: " + "; ".join(source_errors[:10])
        )

    approval = {
        "approval_schema_version": "software_metric_v3_approval_v1",
        "release_id": RELEASE_ID,
        "approved_by": reviewer,
        "approved_at_utc": reviewed_at,
        "approved_workbook_path": str(proposal_path),
        "approved_workbook_sha256": expected_hash,
        "approval_source": "direct_user_instruction",
        "proposal_row_count": len(proposal_rows),
        "decision_counts": dict(
            sorted(Counter(row["decision"] for row in approved_rows).items())
        ),
        "calibration_eligible_count": sum(
            int(row["calibration_eligible_flag"]) for row in approved_rows
        ),
        "production_weight_modified_flag": 0,
    }
    corpus = {
        "corpus_schema_version": "software_metrics_v3_expansion_corpus_v1",
        "release_id": RELEASE_ID,
        "approved_workbook_sha256": expected_hash,
        "observation_count": len(approved_rows),
        "observations": [
            {
                "source_evidence": expansion_source[
                    str(row["source_evidence_key"])
                ],
                "approved_review": row,
            }
            for row in approved_rows
        ],
    }
    if args.write:
        atomic_csv(official_path, approved_rows)
        if file_sha256(official_path) != official_sha256:
            raise RuntimeError("Official review hash changed during write")
        atomic_json(approval_path, approval)
        atomic_json(corpus_path, corpus)
        atomic_json(policy_path, policy)
        atomic_csv(policy_csv_path, policy_csv_rows(policy["decisions"]))
        load_policy(policy_path)

    manifest: dict[str, Any] = {
        "manifest_version": "software_metric_v3_release_manifest_v1",
        "release_id": RELEASE_ID,
        "execution_mode": "write" if args.write else "validate_only",
        "reviewer": reviewer,
        "reviewed_at_utc": reviewed_at,
        "approved_workbook_sha256": expected_hash,
        "official_review_sha256": official_sha256,
        "base_decision_count": len(base_policy["decisions"]),
        "expansion_decision_count": len(expansion_decisions),
        "cumulative_decision_count": len(policy["decisions"]),
        "decision_counts": policy["decision_counts"],
        "calibration_eligible_expansion_count": approval[
            "calibration_eligible_count"
        ],
        "review_validation_status": review_summary["validation_status"],
        "policy_validation_status": "PASS",
        "source_integrity_status": "PASS",
        "measurement_only_flag": 1,
        "production_weight_modified_flag": 0,
    }
    if args.write:
        manifest["artifacts"] = {
            "official_review": _artifact(official_path),
            "approval": _artifact(approval_path),
            "expansion_corpus": _artifact(corpus_path),
            "policy_json": _artifact(policy_path),
            "policy_csv": _artifact(policy_csv_path),
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(
            output_dir / "software_metrics_v3_release_manifest.json",
            manifest,
        )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
