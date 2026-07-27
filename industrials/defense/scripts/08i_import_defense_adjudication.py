#!/usr/bin/env python3
"""Import reviewed pair-adjudication decisions into the parser review-policy registry.

The adjudication queue carries PAIR-level decisions (ACCEPT/REJECT/STRUCTURAL_NA/
DEFER) while the production registry requires EVIDENCE-level rows (accession,
document, concept, value, unit, period). This importer bridges the two:

  * verifies the sealed source queue and evidence package hashes,
  * allows edits only in the review columns of the reviewed copy,
  * resolves every selected_evidence_key uniquely against the evidence package,
  * maps ACCEPT -> ACCEPTED, REJECT -> REJECTED_POLICY (one row per rejected
    key), STRUCTURAL_NA -> STRUCTURAL_NA, DEFER -> no row (promotion blocker),
  * generates deterministic policy ids, preserves existing registry rows, and
    validates the merged candidate registry with load_review_policies (which
    enforces duplicate/overlap rejection),
  * writes a candidate policy CSV plus a sealed import manifest; --apply copies
    the validated candidate over the live registry.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.policy import POLICY_FIELDS, load_review_policies  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.defense.research_artifacts import sha256_file, utc_now, write_json_atomic  # noqa: E402

MODEL_FAMILY = "defense"
REVIEW_COLUMNS = (
    "review_decision",
    "selected_evidence_key",
    "decision_reason",
    "review_notes",
    "reviewed_by",
    "reviewed_at",
)
DECISION_MAP = {
    "ACCEPT": "ACCEPTED",
    "REJECT": "REJECTED_POLICY",
    "STRUCTURAL_NA": "STRUCTURAL_NA",
    "DEFER": "",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import reviewed defense pair adjudications into the review-policy registry.")
    default_dir = PROJECT_ROOT / "output" / "industrials" / "defense" / "dedicated_parser" / "2026-07-24"
    parser.add_argument("--source-queue", type=Path, default=default_dir / "defense_specialized_metric_pair_adjudication_queue.csv")
    parser.add_argument("--reviewed-queue", type=Path, required=True)
    parser.add_argument("--evidence-csv", type=Path, default=default_dir / "defense_specialized_metric_evidence_review.csv")
    parser.add_argument("--registry", type=Path, default=PACKAGE_ROOT / "defense" / "review_policies" / "dedicated_parser_review_policy.csv")
    parser.add_argument("--policy-version", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--expected-source-queue-sha256", default="")
    parser.add_argument("--expected-evidence-sha256", default="")
    parser.add_argument("--apply", action="store_true", help="Copy the validated candidate registry over the live registry.")
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{str(k): str(v or "") for k, v in row.items()} for row in csv.DictReader(handle)]


def identity_fingerprint(row: dict[str, str]) -> str:
    frozen = {k: v for k, v in row.items() if k not in REVIEW_COLUMNS}
    payload = json.dumps(frozen, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def deterministic_policy_id(*, run_id: str, evidence_key: str, decision: str) -> str:
    digest = hashlib.sha256(f"{MODEL_FAMILY}|{run_id}|{evidence_key}|{decision}".encode("utf-8")).hexdigest()[:14]
    return f"dprev_r{run_id}_{digest}"


def validate_reviewed_at(raw: str, *, context: str) -> str:
    text = raw.strip()
    if not text:
        raise ValueError(f"{context}: reviewed_at is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{context}: reviewed_at must be timezone-aware, got {raw!r}")
    return text


def build_policy_row(
    *,
    evidence: dict[str, str],
    decision: str,
    queue_row: dict[str, str],
    policy_version: str,
    run_id: str,
) -> dict[str, str]:
    reason = queue_row["decision_reason"].strip()
    if not reason:
        raise ValueError(f"{queue_row['ticker']}/{queue_row['metric_name']}: decision_reason is required")
    reviewed_by = queue_row["reviewed_by"].strip()
    if not reviewed_by:
        raise ValueError(f"{queue_row['ticker']}/{queue_row['metric_name']}: reviewed_by is required")
    reviewed_at = validate_reviewed_at(queue_row["reviewed_at"], context=f"{queue_row['ticker']}/{queue_row['metric_name']}")
    evidence_key = evidence["evidence_key"]
    return {
        "policy_id": deterministic_policy_id(run_id=run_id, evidence_key=evidence_key, decision=decision),
        "policy_version": policy_version,
        "enabled": "1",
        "model_family": MODEL_FAMILY,
        "ticker": evidence["ticker"].upper(),
        "accession_number": evidence["accession_number"],
        "source_document": evidence["source_document"],
        "metric_name": evidence["metric_name"],
        "concept_name": evidence.get("concept_name", ""),
        "candidate_value": evidence.get("candidate_value", ""),
        "value_tolerance": "0.000001",
        "unit": evidence.get("unit", ""),
        "period_start": evidence.get("period_start", ""),
        "period_end": evidence.get("period_end", ""),
        "decision": decision,
        "status_reason": reason,
        "scope_override": "",
        "confidence_override": "",
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
        "period_start_override": "",
        "period_end_override": "",
        "value_override": "",
    }


def structural_na_row(*, queue_row: dict[str, str], policy_version: str, run_id: str) -> dict[str, str]:
    accession = queue_row.get("representative_accession_number", "").strip()
    document = queue_row.get("representative_source_document", "").strip()
    period_end = queue_row.get("representative_period_end", "").strip() or queue_row.get("asof_date", "").strip()
    if not accession or not document:
        raise ValueError(
            f"{queue_row['ticker']}/{queue_row['metric_name']}: STRUCTURAL_NA needs representative accession/document "
            "(or a selected_evidence_key) to anchor the policy row"
        )
    synthetic = {
        "evidence_key": f"structural_na::{queue_row['ticker']}::{queue_row['metric_name']}::{accession}",
        "ticker": queue_row["ticker"],
        "accession_number": accession,
        "source_document": document,
        "metric_name": queue_row["metric_name"],
        "concept_name": "",
        "candidate_value": "",
        "unit": "",
        "period_start": "",
        "period_end": period_end,
    }
    return build_policy_row(evidence=synthetic, decision="STRUCTURAL_NA", queue_row=queue_row, policy_version=policy_version, run_id=run_id)


def main() -> int:
    args = parse_args()
    source_queue = args.source_queue.expanduser().resolve()
    reviewed_queue = args.reviewed_queue.expanduser().resolve()
    evidence_csv = args.evidence_csv.expanduser().resolve()
    registry = args.registry.expanduser().resolve()
    output_dir = (args.output_dir.expanduser().resolve() if args.output_dir else reviewed_queue.parent)
    for path in (source_queue, reviewed_queue, evidence_csv, registry):
        if not path.exists():
            raise FileNotFoundError(path)

    source_sha = sha256_file(source_queue)
    evidence_sha = sha256_file(evidence_csv)
    if args.expected_source_queue_sha256 and args.expected_source_queue_sha256 != source_sha:
        raise RuntimeError(f"Source queue hash mismatch: expected {args.expected_source_queue_sha256} got {source_sha}")
    if args.expected_evidence_sha256 and args.expected_evidence_sha256 != evidence_sha:
        raise RuntimeError(f"Evidence package hash mismatch: expected {args.expected_evidence_sha256} got {evidence_sha}")

    source_rows = read_rows(source_queue)
    reviewed_rows = read_rows(reviewed_queue)
    if len(source_rows) != len(reviewed_rows):
        raise ValueError(f"Reviewed queue row count {len(reviewed_rows)} != sealed source {len(source_rows)}")
    tampered: list[str] = []
    for idx, (src, rev) in enumerate(zip(source_rows, reviewed_rows), start=2):
        if identity_fingerprint(src) != identity_fingerprint(rev):
            tampered.append(f"row {idx} ({rev.get('ticker')}/{rev.get('metric_name')})")
    if tampered:
        raise ValueError("Reviewed queue modified outside review columns: " + "; ".join(tampered[:10]))

    evidence_by_key: dict[str, dict[str, str]] = {}
    dupes: set[str] = set()
    with evidence_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = str(row.get("evidence_key") or "")
            if not key:
                continue
            if key in evidence_by_key:
                dupes.add(key)
            evidence_by_key[key] = {str(k): str(v or "") for k, v in row.items()}

    run_ids = {r.get("run_id", "") for r in reviewed_rows if r.get("run_id", "")}
    run_id = sorted(run_ids)[-1] if run_ids else "0"
    policy_version = args.policy_version or f"{MODEL_FAMILY}_run{run_id}_adjudication_v1"

    new_rows: list[dict[str, str]] = []
    errors: list[str] = []
    decision_counts: Counter[str] = Counter()
    for row in reviewed_rows:
        decision_raw = row.get("review_decision", "").strip().upper()
        if not decision_raw:
            decision_counts["(blank)"] += 1
            continue
        if decision_raw not in DECISION_MAP:
            errors.append(f"{row['ticker']}/{row['metric_name']}: unsupported review_decision {decision_raw!r}")
            continue
        decision_counts[decision_raw] += 1
        if decision_raw == "DEFER":
            continue
        keys = [k.strip() for k in row.get("selected_evidence_key", "").replace("|", ";").split(";") if k.strip()]
        try:
            if decision_raw == "ACCEPT":
                if len(keys) != 1:
                    raise ValueError(f"{row['ticker']}/{row['metric_name']}: ACCEPT requires exactly one selected_evidence_key, got {len(keys)}")
                evidence = evidence_by_key.get(keys[0])
                if evidence is None:
                    raise ValueError(f"{row['ticker']}/{row['metric_name']}: selected_evidence_key not found: {keys[0]}")
                if keys[0] in dupes:
                    raise ValueError(f"{row['ticker']}/{row['metric_name']}: evidence key is not unique in package: {keys[0]}")
                new_rows.append(build_policy_row(evidence=evidence, decision="ACCEPTED", queue_row=row, policy_version=policy_version, run_id=run_id))
            elif decision_raw == "REJECT":
                if not keys:
                    raise ValueError(f"{row['ticker']}/{row['metric_name']}: REJECT requires at least one selected_evidence_key")
                for key in keys:
                    evidence = evidence_by_key.get(key)
                    if evidence is None:
                        raise ValueError(f"{row['ticker']}/{row['metric_name']}: rejected evidence_key not found: {key}")
                    if key in dupes:
                        raise ValueError(f"{row['ticker']}/{row['metric_name']}: evidence key is not unique in package: {key}")
                    new_rows.append(build_policy_row(evidence=evidence, decision="REJECTED_POLICY", queue_row=row, policy_version=policy_version, run_id=run_id))
            else:  # STRUCTURAL_NA
                if keys:
                    evidence = evidence_by_key.get(keys[0])
                    if evidence is None:
                        raise ValueError(f"{row['ticker']}/{row['metric_name']}: selected_evidence_key not found: {keys[0]}")
                    new_rows.append(build_policy_row(evidence=evidence, decision="STRUCTURAL_NA", queue_row=row, policy_version=policy_version, run_id=run_id))
                else:
                    new_rows.append(structural_na_row(queue_row=row, policy_version=policy_version, run_id=run_id))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        for message in errors[:25]:
            print(f"ERROR: {message}", file=sys.stderr)
        raise SystemExit(f"Import blocked: {len(errors)} adjudication error(s).")

    existing_rows = read_rows(registry)
    existing_ids = {r["policy_id"] for r in existing_rows}
    clashing = [r["policy_id"] for r in new_rows if r["policy_id"] in existing_ids]
    if clashing:
        raise SystemExit(f"Import blocked: policy ids already present in registry: {clashing[:5]}")

    def exact_key(row: dict[str, str]) -> tuple[str, ...]:
        # Mirrors load_review_policies' exact-key: two evidence keys that share
        # this tuple (e.g. the same value extracted twice from one document)
        # are one policy; keep the first and drop the redundant duplicates.
        return (
            row["model_family"], row["ticker"], row["accession_number"], row["source_document"],
            row["metric_name"], row["concept_name"], row["candidate_value"], row["unit"],
            row["period_start"], row["period_end"],
        )

    seen_keys = {exact_key(r) for r in existing_rows}
    deduped_rows: list[dict[str, str]] = []
    dropped_duplicates = 0
    for row in new_rows:
        key = exact_key(row)
        if key in seen_keys:
            dropped_duplicates += 1
            continue
        seen_keys.add(key)
        deduped_rows.append(row)
    if dropped_duplicates:
        print(f"Deduplicated {dropped_duplicates} policy rows sharing an exact evidence tuple.")
    new_rows = deduped_rows
    merged = existing_rows + new_rows

    candidate_path = output_dir / "dedicated_parser_review_policy_candidate.csv"
    write_csv_atomic(candidate_path, list(POLICY_FIELDS), merged)
    # Full-contract validation (schema, dupes, tolerance-overlaps, timestamps).
    policies = load_review_policies(candidate_path)

    manifest = {
        "artifact_family": "defense_adjudication_import",
        "model_family": MODEL_FAMILY,
        "created_at_utc": utc_now(),
        "run_id": run_id,
        "policy_version": policy_version,
        "source_queue": str(source_queue),
        "source_queue_sha256": source_sha,
        "reviewed_queue": str(reviewed_queue),
        "reviewed_queue_sha256": sha256_file(reviewed_queue),
        "evidence_csv": str(evidence_csv),
        "evidence_csv_sha256": evidence_sha,
        "registry_before": str(registry),
        "registry_before_sha256": sha256_file(registry),
        "decision_counts": dict(decision_counts),
        "existing_policy_rows": len(existing_rows),
        "new_policy_rows": len(new_rows),
        "deduplicated_policy_rows": dropped_duplicates,
        "enabled_policies_after_merge": len(policies),
        "candidate_registry": str(candidate_path),
        "candidate_registry_sha256": sha256_file(candidate_path),
        "applied": bool(args.apply),
    }
    if args.apply:
        shutil.copyfile(candidate_path, registry)
        manifest["registry_after_sha256"] = sha256_file(registry)
    write_json_atomic(output_dir / "dedicated_parser_adjudication_import_manifest.json", manifest)
    print(
        f"Import OK: decisions={dict(decision_counts)} new_policy_rows={len(new_rows)} "
        f"enabled_after_merge={len(policies)} applied={bool(args.apply)}"
    )
    print(f"Candidate registry: {candidate_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
