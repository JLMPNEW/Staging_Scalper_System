#!/usr/bin/env python3
"""Audit exact period-start recovery feasibility without changing source data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.period_start_feasibility import (  # noqa: E402
    classify_candidate_period_start,
    classify_conflict_group,
)


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_CONFLICT_AUDIT = (
    ROOT
    / "investable_v5"
    / "fact_conflict_resolution_v3"
    / "2026-08-25"
    / "v2"
    / "transportation_v8_fact_conflict_audit.json"
)
DEFAULT_OUTPUT_ROOT = (
    ROOT / "investable_v5" / "period_start_recovery_feasibility_v1"
)

CANDIDATE_FIELDS = (
    "conflict_id",
    "ticker",
    "metric_id",
    "period_end",
    "available_on",
    "candidate_key",
    "source_lane",
    "concept_name",
    "source_document",
    "candidate_period_start",
    "bound_evidence_period_start",
    "bound_provenance_period_starts_json",
    "bound_xbrl_links_json",
    "duration_phrase_flag",
    "explicit_full_date_range_flag",
    "semantic_table_locator_flag",
    "table_context_hash_flag",
    "exact_recoverable_flag",
    "effective_period_start",
    "recovery_reason",
)
GROUP_FIELDS = (
    "conflict_id",
    "ticker",
    "metric_id",
    "period_end",
    "available_on",
    "original_residual_classification",
    "candidate_count",
    "missing_candidate_count",
    "exact_candidate_recovery_count",
    "known_period_starts_json",
    "group_exact_recoverable_flag",
    "exact_recovered_period_start",
    "feasibility_category",
)
SNAPSHOT_FIELDS = (
    "evidence_key",
    "run_id",
    "ticker",
    "accession_number",
    "metric_name",
    "candidate_value",
    "unit",
    "period_start",
    "period_end",
    "scope",
    "evidence_text",
    "source_document",
    "extraction_method",
    "provenance_json",
    "parser_release",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", required=True)
    parser.add_argument(
        "--conflict-audit",
        type=Path,
        default=DEFAULT_CONFLICT_AUDIT,
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
    if not reader.fieldnames:
        raise ValueError(f"{path}: CSV header is missing")
    return rows


def read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_evidence(
    connection: sqlite3.Connection,
    evidence_keys: Iterable[str],
) -> dict[str, dict[str, object]]:
    keys = sorted({str(key) for key in evidence_keys if str(key)})
    output: dict[str, dict[str, object]] = {}
    for offset in range(0, len(keys), 500):
        batch = keys[offset : offset + 500]
        marks = ",".join("?" for _ in batch)
        for row in connection.execute(
            "SELECT * FROM sec_parser_metric_evidence_shadow "
            f"WHERE evidence_key IN ({marks})",
            batch,
        ):
            output[str(row["evidence_key"])] = dict(row)
    return output


def evidence_snapshot_sha256(
    evidence: Mapping[str, Mapping[str, object]],
) -> str:
    payload = [
        {field: evidence[key].get(field) for field in SNAPSHOT_FIELDS}
        for key in sorted(evidence)
    ]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def artifact_path(
    manifest: Mapping[str, object],
    name: str,
) -> Path:
    artifacts = manifest.get("artifacts") or {}
    item = artifacts.get(name) if isinstance(artifacts, Mapping) else None
    if not isinstance(item, Mapping):
        raise ValueError(f"conflict audit artifact is missing: {name}")
    path = Path(str(item.get("path") or "")).resolve()
    expected_hash = str(item.get("sha256") or "")
    if not path.is_file() or file_sha256(path) != expected_hash:
        raise ValueError(f"conflict audit artifact hash mismatch: {name}")
    return path


def nested_counts(
    rows: Iterable[Mapping[str, object]],
    *,
    category_field: str,
) -> dict[str, dict[str, int]]:
    counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        counts[str(row.get("metric_id") or "")][
            str(row.get(category_field) or "")
        ] += 1
    return {
        metric: dict(sorted(categories.items()))
        for metric, categories in sorted(counts.items())
    }


def main() -> int:
    args = parse_args()
    conflict_path = args.conflict_audit.expanduser().resolve()
    if not conflict_path.is_file():
        raise FileNotFoundError(f"conflict audit is missing: {conflict_path}")
    conflict = read_json(conflict_path)
    if conflict.get("policy_version") != (
        "transportation_accepted_fact_conflict_resolution_v3"
    ):
        raise ValueError("period-start feasibility requires strict conflict policy v3")
    group_path = artifact_path(conflict, "group_audit")
    evidence_path = artifact_path(conflict, "evidence_audit")
    surface_path = artifact_path(conflict, "surface_normalized_replay")
    tanker_path = artifact_path(conflict, "tanker_normalized_replay")
    groups = read_csv(group_path)
    evidence_rows = read_csv(evidence_path)
    replay_rows = read_csv(surface_path) + read_csv(tanker_path)
    if len(groups) != int(conflict.get("resolver_conflict_count_after") or -1):
        raise ValueError("group audit count does not match the conflict manifest")

    source_db = conflict.get("source_database") or {}
    if not isinstance(source_db, Mapping):
        raise ValueError("conflict audit source_database contract is missing")
    db_path = Path(str(source_db.get("path") or "")).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"bound source database is missing: {db_path}")
    all_evidence_keys = {
        str(row.get("evidence_key") or "")
        for row in replay_rows
        if str(row.get("evidence_key") or "")
    }
    connection = read_only_connection(db_path)
    try:
        bound_evidence = load_evidence(connection, all_evidence_keys)
    finally:
        connection.close()
    missing_evidence = sorted(all_evidence_keys - set(bound_evidence))
    if missing_evidence:
        raise ValueError(
            "bound parser evidence is missing from the read-only database: "
            f"count={len(missing_evidence)}"
        )
    snapshot_hash = evidence_snapshot_sha256(bound_evidence)
    if snapshot_hash != conflict.get("source_evidence_snapshot_sha256"):
        raise ValueError(
            "bound parser evidence snapshot changed after conflict audit"
        )

    group_source = {row["conflict_id"]: row for row in groups}
    candidates_by_group: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    candidate_output: list[dict[str, object]] = []
    for row in evidence_rows:
        conflict_id = str(row.get("conflict_id") or "")
        if conflict_id not in group_source:
            raise ValueError(f"unknown candidate conflict_id={conflict_id}")
        candidate_key = str(row.get("candidate_key") or "")
        database_start = ""
        if str(row.get("source_lane") or "") == "parser_run_evidence":
            database_start = str(
                (bound_evidence.get(candidate_key) or {}).get("period_start")
                or ""
            )
        classification = classify_candidate_period_start(
            row,
            bound_evidence_period_start=database_start,
        )
        item = {
            field: row.get(field, "")
            for field in (
                "conflict_id",
                "ticker",
                "metric_id",
                "period_end",
                "available_on",
                "candidate_key",
                "source_lane",
                "concept_name",
                "source_document",
            )
        } | classification
        candidate_output.append(item)
        candidates_by_group[conflict_id].append(item)

    group_output: list[dict[str, object]] = []
    for conflict_id in sorted(group_source):
        source = group_source[conflict_id]
        classification = classify_conflict_group(
            candidates_by_group[conflict_id]
        )
        group_output.append(
            {
                field: source.get(field, "")
                for field in (
                    "conflict_id",
                    "ticker",
                    "metric_id",
                    "period_end",
                    "available_on",
                )
            }
            | {
                "original_residual_classification": source.get(
                    "residual_classification",
                    "",
                )
            }
            | classification
        )

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else (DEFAULT_OUTPUT_ROOT / args.asof).resolve()
    )
    candidate_path = output_dir / (
        "transportation_v8_period_start_candidate_feasibility.csv"
    )
    group_output_path = output_dir / (
        "transportation_v8_period_start_group_feasibility.csv"
    )
    manifest_path = output_dir / (
        "transportation_v8_period_start_recovery_feasibility.json"
    )
    existing = [
        path
        for path in (candidate_path, group_output_path, manifest_path)
        if path.exists()
    ]
    if existing:
        raise FileExistsError(
            "period-start feasibility artifacts are immutable; choose a new "
            f"--output-dir: {existing}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(candidate_path, CANDIDATE_FIELDS, candidate_output)
    write_csv_atomic(group_output_path, GROUP_FIELDS, group_output)

    candidate_reasons = Counter(
        str(row["recovery_reason"]) for row in candidate_output
    )
    group_categories = Counter(
        str(row["feasibility_category"]) for row in group_output
    )
    exact_candidate_count = sum(
        int(row["exact_recoverable_flag"]) for row in candidate_output
    )
    exact_group_count = sum(
        int(row["group_exact_recoverable_flag"]) for row in group_output
    )
    manifest = {
        "execution_acceptance": "PASS",
        "contract_version": (
            "transportation_period_start_recovery_feasibility_v1"
        ),
        "asof_date": args.asof,
        "audit_scope": (
            "read_only_exact_recovery_from_already_bound_candidate_evidence_"
            "provenance_and_xbrl_identifiers"
        ),
        "inference_policy": (
            "calendar_subtraction_fiscal_calendar_assumptions_numeric_"
            "similarity_and_unlinked_table_dates_are_not_exact_recovery"
        ),
        "source_conflict_count": len(group_output),
        "source_candidate_count": len(candidate_output),
        "missing_period_start_candidate_count": sum(
            not str(row["candidate_period_start"])
            for row in candidate_output
        ),
        "exact_recoverable_candidate_count": exact_candidate_count,
        "exact_recoverable_conflict_group_count": exact_group_count,
        "candidate_count_by_recovery_reason": dict(
            sorted(candidate_reasons.items())
        ),
        "conflict_count_by_feasibility_category": dict(
            sorted(group_categories.items())
        ),
        "candidate_recovery_reason_by_metric": nested_counts(
            candidate_output,
            category_field="recovery_reason",
        ),
        "group_feasibility_by_metric": nested_counts(
            group_output,
            category_field="feasibility_category",
        ),
        "duration_only_candidate_count": sum(
            int(row["duration_phrase_flag"]) for row in candidate_output
            if not str(row["candidate_period_start"])
        ),
        "semantic_table_locator_candidate_count": sum(
            int(row["semantic_table_locator_flag"])
            for row in candidate_output
            if not str(row["candidate_period_start"])
        ),
        "table_context_hash_candidate_count": sum(
            int(row["table_context_hash_flag"])
            for row in candidate_output
            if not str(row["candidate_period_start"])
        ),
        "explicit_full_date_range_candidate_count": sum(
            int(row["explicit_full_date_range_flag"])
            for row in candidate_output
            if not str(row["candidate_period_start"])
        ),
        "source_database": {
            "path": str(db_path),
            "read_only": True,
            "write_count": 0,
        },
        "source_evidence_snapshot_sha256": snapshot_hash,
        "source_evidence_snapshot_hash_verified": True,
        "lineage": {
            "conflict_audit": {
                "path": str(conflict_path),
                "sha256": file_sha256(conflict_path),
            },
            "group_audit": {
                "path": str(group_path),
                "sha256": file_sha256(group_path),
            },
            "evidence_audit": {
                "path": str(evidence_path),
                "sha256": file_sha256(evidence_path),
            },
        },
        "artifacts": {
            "candidate_feasibility": {
                "path": str(candidate_path),
                "sha256": file_sha256(candidate_path),
            },
            "group_feasibility": {
                "path": str(group_output_path),
                "sha256": file_sha256(group_output_path),
            },
        },
        "canonical_fact_mutation_count": 0,
        "speculative_recovery_implemented": False,
        "production_promotion_eligible": False,
        "production_activation_authorized": False,
        "next_gate": (
            "NO_AUTOMATIC_RECOVERY;REQUIRE_EXACT_SOURCE_LINKAGE_OR_MANUAL_"
            "ADJUDICATION_BEFORE_ANY_NEW_REPLAY"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
