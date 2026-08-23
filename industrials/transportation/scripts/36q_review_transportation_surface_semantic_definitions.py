#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG  # noqa: E402
from industrials.transportation.surface_semantic_review import (  # noqa: E402
    REVIEW_POLICY_VERSION,
    candidate_key,
    definition_id,
    definition_signature,
    review_candidate,
)


DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v3" / "surface_delta"
)
QUEUE_NAME = "transportation_surface_semantic_review_queue.csv"
FACT_NAME = "transportation_surface_fact_store_ratio_candidates.csv"
DEFINITION_FIELDS = (
    "definition_id", "review_priority", "source_lane", "run_id", "asof_date", "ticker",
    "metric_id", "concept_name", "unit", "extraction_method", "status_reason", "formula",
    "numerator_concept", "denominator_concept", "represented_candidate_count",
    "represented_period_count", "integrity_pass_count", "semantic_pass_count",
    "semantic_reject_count", "review_decision", "row_filter_required", "review_notes",
    "review_policy_version", "reviewed_by", "reviewed_at",
)
ROW_FIELDS = (
    "definition_id", "candidate_key", "source_lane", "run_id", "asof_date", "ticker",
    "metric_id", "candidate_value", "reviewed_value", "unit", "period_start", "period_end",
    "filing_date", "accepted_at", "form_type", "accession_number", "concept_name",
    "extraction_method", "status_reason", "formula", "numerator_concept",
    "denominator_concept", "definition_basis", "comparability_class", "segment_id",
    "denominator_basis", "weighting_basis", "capacity_basis",
    "source_document", "source_path", "source_content_sha256",
    "evidence_key", "evidence_text_sha256", "source_integrity_pass", "semantic_guard_pass",
    "row_decision", "row_reason", "review_policy_version", "reviewed_by", "reviewed_at",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review selected surface semantic definitions once.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--reviewed-at", required=True)
    parser.add_argument("--reviewed-by", default="transportation_semantic_policy_v1")
    parser.add_argument(
        "--priorities",
        default="HIGH",
        help="Comma-separated queue priorities to review (HIGH,MEDIUM,LOW)",
    )
    parser.add_argument(
        "--expected-definition-count",
        type=int,
        default=0,
        help="Optional immutable count gate for the selected definitions",
    )
    parser.add_argument("--queue", type=Path, default=None)
    parser.add_argument("--fact-store-candidates", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _json(value: object) -> dict[str, object]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parser_rows(connection: sqlite3.Connection, run_id: int) -> list[dict[str, object]]:
    rows = connection.execute(
        "SELECT evidence.*, "
        "(SELECT catalog.source_path FROM sec_parser_document_catalog AS catalog "
        " WHERE catalog.cik=evidence.cik AND catalog.accession_number=evidence.accession_number "
        " AND catalog.document_name=evidence.source_document "
        " ORDER BY catalog.cataloged_at DESC LIMIT 1) AS source_path, "
        "(SELECT catalog.content_sha256 FROM sec_parser_document_catalog AS catalog "
        " WHERE catalog.cik=evidence.cik AND catalog.accession_number=evidence.accession_number "
        " AND catalog.document_name=evidence.source_document "
        " ORDER BY catalog.cataloged_at DESC LIMIT 1) AS source_content_sha256 "
        "FROM sec_parser_run_metric_evidence AS relation "
        "JOIN sec_parser_metric_evidence_shadow AS evidence ON evidence.evidence_key=relation.evidence_key "
        "WHERE relation.run_id=? AND evidence.candidate_value IS NOT NULL "
        "AND UPPER(evidence.candidate_status) IN ('REVIEW','REVIEW_REQUIRED','PENDING_REVIEW')",
        (run_id,),
    ).fetchall()
    output: list[dict[str, object]] = []
    for evidence in rows:
        provenance = _json(evidence["provenance_json"])
        output.append({
            "source_lane": "parser_run_evidence", "run_id": run_id,
            "ticker": evidence["ticker"], "metric_id": evidence["metric_name"],
            "candidate_value": evidence["candidate_value"], "unit": evidence["unit"],
            "period_start": evidence["period_start"], "period_end": evidence["period_end"],
            "filing_date": evidence["filing_date"], "accepted_at": evidence["accepted_at"],
            "form_type": evidence["form_type"], "accession_number": evidence["accession_number"],
            "concept_name": evidence["concept_name"], "extraction_method": evidence["extraction_method"],
            "status_reason": evidence["status_reason"], "formula": provenance.get("formula", ""),
            "numerator_concept": provenance.get("numerator_concept", ""),
            "denominator_concept": provenance.get("denominator_concept", ""),
            "source_document": evidence["source_document"], "source_path": evidence["source_path"],
            "source_content_sha256": evidence["source_content_sha256"],
            "evidence_key": evidence["evidence_key"], "evidence_text": evidence["evidence_text"],
            "provenance_json": evidence["provenance_json"],
        })
    return output


def _fact_rows(path: Path, run_id: int) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for row in _csv(path):
        output.append({
            "source_lane": "fact_store_ratio", "run_id": run_id, "ticker": row["ticker"],
            "metric_id": row["metric_id"], "candidate_value": row["value"], "unit": row["unit"],
            "period_start": row["period_start"], "period_end": row["period_end"],
            "filing_date": row["filing_date"], "accepted_at": row["accepted_at"],
            "form_type": row["form_type"], "accession_number": row["accession_number"],
            "concept_name": row["numerator_concept"],
            "extraction_method": "loaded_sec_fact_store_ratio", "status_reason": row["reason"],
            "formula": row["formula"], "numerator_concept": row["numerator_concept"],
            "denominator_concept": row["denominator_concept"], "source_document": row["source_id"],
            "source_path": "", "source_content_sha256": "", "evidence_key": "", "evidence_text": "",
            "provenance_json": json.dumps({
                "currency": row["currency"], "numerator_value": row["numerator_value"],
                "denominator_value": row["denominator_value"],
                "fact_store_recovery_version": row["fact_store_recovery_version"],
            }, sort_keys=True),
        })
    return output


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else DEFAULT_OUTPUT_ROOT / args.asof
    queue_path = args.queue.expanduser().resolve() if args.queue else output_dir / QUEUE_NAME
    fact_path = args.fact_store_candidates.expanduser().resolve() if args.fact_store_candidates else output_dir / FACT_NAME
    output_dir.mkdir(parents=True, exist_ok=True)

    priorities = {
        value.strip().upper()
        for value in args.priorities.split(",")
        if value.strip()
    }
    if not priorities or not priorities <= {"HIGH", "MEDIUM", "LOW"}:
        raise ValueError(f"invalid review priorities={sorted(priorities)}")
    definitions = [row for row in _csv(queue_path) if row["review_priority"] in priorities]
    if args.expected_definition_count and len(definitions) != args.expected_definition_count:
        raise ValueError(
            f"expected {args.expected_definition_count} selected definitions; found {len(definitions)}"
        )
    ids = {definition_id(row) for row in definitions}
    if len(ids) != len(definitions):
        raise ValueError("selected definition identifiers are not unique")

    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    run = connection.execute("SELECT * FROM sec_parser_run WHERE run_id=?", (args.run_id,)).fetchone()
    if run is None or str(run["asof_date"]) != args.asof or str(run["status"]) != "COMPLETED":
        raise ValueError("review run must be the completed parser run used by the queue")

    raw = _parser_rows(connection, args.run_id) + _fact_rows(fact_path, args.run_id)
    wanted = {definition_signature(row) for row in definitions}
    grouped: defaultdict[tuple[str, ...], list[dict[str, object]]] = defaultdict(list)
    for row in raw:
        signature = definition_signature(row)
        if signature in wanted:
            grouped[signature].append(row)

    hash_cache: dict[str, str] = {}
    row_reviews: list[dict[str, object]] = []
    rows_by_definition: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    for definition in definitions:
        signature = definition_signature(definition)
        candidates = grouped.get(signature, [])
        expected = int(definition["represented_candidate_count"])
        if len(candidates) != expected:
            raise ValueError(
                f"definition candidate count changed for {definition['ticker']} {definition['metric_id']}: "
                f"expected {expected}, found {len(candidates)}"
            )
        did = definition_id(definition)
        for row in candidates:
            lane = str(row["source_lane"])
            provenance = _json(row.get("provenance_json"))
            integrity = True
            integrity_reason = "lineage_fields_pass"
            if lane == "parser_run_evidence":
                path_text = str(row.get("source_path") or "")
                stored_hash = str(row.get("source_content_sha256") or "")
                provenance_hash = str(provenance.get("document_sha256") or "")
                catalog_bound_xbrl = bool(
                    not provenance_hash
                    and provenance.get("context_id")
                    and provenance.get("raw_xbrl_unit")
                    and row.get("accession_number")
                    and row.get("source_document")
                )
                integrity = bool(
                    path_text
                    and stored_hash
                    and (provenance_hash == stored_hash or catalog_bound_xbrl)
                )
                if integrity:
                    if path_text not in hash_cache:
                        path = Path(path_text)
                        hash_cache[path_text] = file_sha256(path) if path.is_file() else "MISSING"
                    integrity = hash_cache[path_text] == stored_hash
                integrity_reason = (
                    "source_hash_pass_catalog_bound_xbrl"
                    if integrity and catalog_bound_xbrl
                    else "source_hash_pass"
                    if integrity
                    else "source_hash_or_catalog_lineage_failed"
                )
            else:
                integrity = bool(row.get("source_document") and row.get("accession_number"))
                integrity_reason = "fact_store_lineage_pass" if integrity else "fact_store_lineage_failed"
            semantic = review_candidate(row)
            approved = integrity and semantic.approved
            reason = semantic.reason if integrity else integrity_reason
            record = {
                "definition_id": did, "candidate_key": candidate_key(row), "source_lane": lane,
                "run_id": args.run_id, "asof_date": args.asof, "ticker": row["ticker"],
                "metric_id": row["metric_id"], "candidate_value": row["candidate_value"],
                "reviewed_value": "" if semantic.reviewed_value is None else semantic.reviewed_value,
                "unit": row["unit"], "period_start": row["period_start"], "period_end": row["period_end"],
                "filing_date": row["filing_date"], "accepted_at": row["accepted_at"],
                "form_type": row["form_type"], "accession_number": row["accession_number"],
                "concept_name": row["concept_name"], "extraction_method": row["extraction_method"],
                "status_reason": row["status_reason"], "formula": row["formula"],
                "numerator_concept": row["numerator_concept"],
                "denominator_concept": row["denominator_concept"],
                "definition_basis": provenance.get("definition_basis") or row.get("formula") or row.get("concept_name") or "",
                "comparability_class": provenance.get("comparability_class") or ("exact_fact_definition" if lane == "fact_store_ratio" else ""),
                "segment_id": provenance.get("segment_id") or "",
                "denominator_basis": provenance.get("denominator_basis") or "",
                "weighting_basis": provenance.get("weighting_basis") or "",
                "capacity_basis": provenance.get("capacity_basis") or "",
                "source_document": row["source_document"], "source_path": row["source_path"],
                "source_content_sha256": row["source_content_sha256"], "evidence_key": row["evidence_key"],
                "evidence_text_sha256": hashlib.sha256(str(row["evidence_text"]).encode("utf-8")).hexdigest(),
                "source_integrity_pass": int(integrity), "semantic_guard_pass": int(semantic.approved),
                "row_decision": "APPROVED" if approved else "REJECTED_POLICY",
                "row_reason": reason, "review_policy_version": REVIEW_POLICY_VERSION,
                "reviewed_by": args.reviewed_by, "reviewed_at": args.reviewed_at,
            }
            row_reviews.append(record)
            rows_by_definition[did].append(record)

    decision_rows: list[dict[str, object]] = []
    for definition in definitions:
        did = definition_id(definition)
        rows = rows_by_definition[did]
        integrity_count = sum(int(row["source_integrity_pass"]) for row in rows)
        pass_count = sum(row["row_decision"] == "APPROVED" for row in rows)
        reject_count = len(rows) - pass_count
        if pass_count:
            decision = "APPROVED"
            note = "definition_has_source_verified_semantic_rows; replay_is_row_guarded"
        elif integrity_count:
            decision = "REJECTED"
            note = "no_represented_candidate_satisfied_the_metric_definition"
        else:
            decision = "MANUAL_REQUIRED"
            note = "source_integrity_prevented_semantic_adjudication"
        decision_rows.append({
            "definition_id": did, "review_priority": definition["review_priority"],
            "source_lane": definition["source_lane"],
            "run_id": args.run_id, "asof_date": args.asof, "ticker": definition["ticker"],
            "metric_id": definition["metric_id"], "concept_name": definition["concept_name"],
            "unit": definition["unit"], "extraction_method": definition["extraction_method"],
            "status_reason": definition["status_reason"], "formula": definition["formula"],
            "numerator_concept": definition["numerator_concept"],
            "denominator_concept": definition["denominator_concept"],
            "represented_candidate_count": len(rows),
            "represented_period_count": definition["represented_period_count"],
            "integrity_pass_count": integrity_count, "semantic_pass_count": pass_count,
            "semantic_reject_count": reject_count, "review_decision": decision,
            "row_filter_required": int(pass_count > 0 and reject_count > 0), "review_notes": note,
            "review_policy_version": REVIEW_POLICY_VERSION, "reviewed_by": args.reviewed_by,
            "reviewed_at": args.reviewed_at,
        })

    decision_rows.sort(key=lambda row: (str(row["metric_id"]), str(row["ticker"]), str(row["definition_id"])))
    row_reviews.sort(key=lambda row: (str(row["definition_id"]), str(row["period_end"]), str(row["candidate_key"])))
    decisions_path = output_dir / "transportation_surface_semantic_definition_decisions.csv"
    rows_path = output_dir / "transportation_surface_semantic_candidate_reviews.csv"
    write_csv_atomic(decisions_path, DEFINITION_FIELDS, decision_rows)
    write_csv_atomic(rows_path, ROW_FIELDS, row_reviews)
    summary: dict[str, Any] = {
        "acceptance": "PASS", "asof_date": args.asof, "run_id": args.run_id,
        "review_policy_version": REVIEW_POLICY_VERSION, "reviewed_by": args.reviewed_by,
        "reviewed_at": args.reviewed_at,
        "reviewed_priorities": sorted(priorities),
        "reviewed_definition_count": len(decision_rows),
        "high_definition_count": sum(
            row["review_priority"] == "HIGH" for row in decision_rows
        ),
        "definition_decision_counts": dict(sorted(Counter(str(row["review_decision"]) for row in decision_rows).items())),
        "candidate_review_count": len(row_reviews),
        "candidate_decision_counts": dict(sorted(Counter(str(row["row_decision"]) for row in row_reviews).items())),
        "physically_hashed_source_document_count": len(hash_cache),
        "source_document_reparse_count": 0, "queue_sha256": file_sha256(queue_path),
        "fact_store_candidates_sha256": file_sha256(fact_path),
        "definition_decisions_csv": str(decisions_path),
        "definition_decisions_sha256": file_sha256(decisions_path),
        "candidate_reviews_csv": str(rows_path), "candidate_reviews_sha256": file_sha256(rows_path),
        "canonical_candidate_mutation": False, "calibration_authorized": False,
        "production_promotion_authorized": False,
        "next_gate": "REPLAY_APPROVED_DEFINITIONS_ONCE",
    }
    write_text_atomic(
        output_dir / "transportation_surface_semantic_review.json",
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
