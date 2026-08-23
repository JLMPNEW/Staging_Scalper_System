#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.tanker_semantic_review import REVIEW_POLICY_VERSION  # noqa: E402


DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v3" / "tanker_delta"
)
REPLAY_FIELDS = (
    "definition_id", "candidate_key", "source_lane", "run_id", "asof_date", "ticker",
    "metric_id", "value", "unit", "period_start", "period_end", "filing_date", "accepted_at",
    "form_type", "accession_number", "concept_name", "formula", "numerator_concept",
    "denominator_concept", "definition_basis", "comparability_class", "segment_id",
    "denominator_basis", "weighting_basis", "capacity_basis",
    "source_document", "source_content_sha256", "evidence_key",
    "replay_status", "replay_reason", "review_policy_version", "reviewed_by", "reviewed_at",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay approved tanker definitions without reparsing documents.")
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--review-manifest", type=Path, default=None)
    return parser.parse_args()


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else DEFAULT_OUTPUT_ROOT / args.asof
    manifest_path = args.review_manifest.expanduser().resolve() if args.review_manifest else output_dir / "transportation_tanker_semantic_review.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("acceptance") != "PASS"
        or manifest.get("asof_date") != args.asof
        or int(manifest.get("run_id") or 0) != args.run_id
        or manifest.get("review_policy_version") != REVIEW_POLICY_VERSION
        or int(manifest.get("reviewed_definition_count") or 0) <= 0
    ):
        raise ValueError("semantic review manifest does not match the requested replay")
    decisions_path = Path(str(manifest["definition_decisions_csv"]))
    reviews_path = Path(str(manifest["candidate_reviews_csv"]))
    if file_sha256(decisions_path) != manifest["definition_decisions_sha256"]:
        raise ValueError("definition decision file hash changed after review")
    if file_sha256(reviews_path) != manifest["candidate_reviews_sha256"]:
        raise ValueError("candidate review file hash changed after review")
    decisions = {row["definition_id"]: row for row in _csv(decisions_path)}
    expected_definition_count = int(manifest["reviewed_definition_count"])
    if len(decisions) != expected_definition_count:
        raise ValueError("replay requires every selected definition decision")

    replay: list[dict[str, object]] = []
    for row in _csv(reviews_path):
        definition = decisions.get(row["definition_id"])
        if definition is None:
            raise ValueError("candidate review references an unknown definition")
        if definition["review_decision"] == "APPROVED" and row["row_decision"] == "APPROVED":
            status = "ACCEPTED"
            reason = "approved_definition_and_source_verified_row_replay"
        elif definition["review_decision"] == "MANUAL_REQUIRED":
            status = "REVIEW_REQUIRED"
            reason = "definition_requires_manual_source_recovery"
        else:
            status = "REJECTED_POLICY"
            reason = row["row_reason"] or "definition_rejected"
        replay.append({
            "definition_id": row["definition_id"], "candidate_key": row["candidate_key"],
            "source_lane": row["source_lane"], "run_id": row["run_id"], "asof_date": row["asof_date"],
            "ticker": row["ticker"], "metric_id": row["metric_id"], "value": row["reviewed_value"],
            "unit": row["unit"], "period_start": row["period_start"], "period_end": row["period_end"],
            "filing_date": row["filing_date"], "accepted_at": row["accepted_at"],
            "form_type": row["form_type"], "accession_number": row["accession_number"],
            "concept_name": row["concept_name"], "formula": row["formula"],
            "numerator_concept": row["numerator_concept"],
            "denominator_concept": row["denominator_concept"],
            "definition_basis": row["definition_basis"],
            "comparability_class": row["comparability_class"],
            "segment_id": row["segment_id"],
            "denominator_basis": row["denominator_basis"],
            "weighting_basis": row["weighting_basis"],
            "capacity_basis": row["capacity_basis"],
            "source_document": row["source_document"],
            "source_content_sha256": row["source_content_sha256"], "evidence_key": row["evidence_key"],
            "replay_status": status, "replay_reason": reason,
            "review_policy_version": row["review_policy_version"], "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
        })
    unique: dict[str, dict[str, object]] = {}
    for row in replay:
        key = str(row["candidate_key"])
        if key in unique and unique[key] != row:
            raise ValueError(f"conflicting replay rows for candidate {key}")
        unique[key] = row
    replay = sorted(unique.values(), key=lambda row: (
        str(row["metric_id"]), str(row["ticker"]), str(row["period_end"]), str(row["candidate_key"])
    ))
    accepted = [row for row in replay if row["replay_status"] == "ACCEPTED"]
    replay_path = output_dir / "transportation_tanker_semantic_replay.csv"
    accepted_path = output_dir / "transportation_tanker_semantic_replay_accepted.csv"
    write_csv_atomic(replay_path, REPLAY_FIELDS, replay)
    write_csv_atomic(accepted_path, REPLAY_FIELDS, accepted)
    summary: dict[str, Any] = {
        "acceptance": "PASS", "asof_date": args.asof, "run_id": args.run_id,
        "review_policy_version": REVIEW_POLICY_VERSION,
        "reviewed_definition_count": len(decisions),
        "high_definition_count": int(manifest.get("high_definition_count") or 0),
        "approved_definition_count": sum(row["review_decision"] == "APPROVED" for row in decisions.values()),
        "replay_candidate_count": len(replay), "accepted_candidate_count": len(accepted),
        "replay_status_counts": dict(sorted(Counter(str(row["replay_status"]) for row in replay).items())),
        "accepted_definition_count": len({str(row["definition_id"]) for row in accepted}),
        "accepted_metric_count": len({str(row["metric_id"]) for row in accepted}),
        "accepted_ticker_count": len({str(row["ticker"]) for row in accepted}),
        "accepted_counts_by_metric": dict(sorted(Counter(str(row["metric_id"]) for row in accepted).items())),
        "review_manifest_sha256": file_sha256(manifest_path), "replay_csv": str(replay_path),
        "replay_csv_sha256": file_sha256(replay_path), "accepted_csv": str(accepted_path),
        "accepted_csv_sha256": file_sha256(accepted_path), "source_document_reparse_count": 0,
        "canonical_candidate_mutation": False, "calibration_authorized": False,
        "production_promotion_authorized": False, "next_gate": "RERUN_DOMAIN_COVERAGE_AUDIT_ONCE",
    }
    write_text_atomic(
        output_dir / "transportation_tanker_semantic_replay.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

