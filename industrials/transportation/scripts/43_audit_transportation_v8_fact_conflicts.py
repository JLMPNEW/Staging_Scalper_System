#!/usr/bin/env python3
"""Audit and safely normalize v8 accepted-fact resolver conflicts.

The source replay CSVs and parser database are read only.  This script writes
new versioned diagnostics and conflict-normalized replay copies; it never
changes an accepted replay, parser database, score artifact, or production
configuration.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import file_sha256  # noqa: E402
from industrials.transportation.fact_conflict_resolution import (  # noqa: E402
    POLICY_VERSION,
    resolve_accepted_fact_conflicts,
)
from industrials.transportation.subgroup_scoring import (  # noqa: E402
    build_fact_history,
    load_subgroup_score_policy,
    resolver_selection_conflict_counts,
)


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_CONFIG = PROJECT_ROOT / "industrials" / "config.yaml"
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "data"
    / "transportation_subgroup_score_policy_v8.yaml"
)
DEFAULT_SURFACE_REPLAY = (
    ROOT
    / "investable_v3"
    / "surface_delta"
    / "2026-08-21"
    / "transportation_surface_semantic_replay_accepted.csv"
)
DEFAULT_TANKER_REPLAY = (
    ROOT
    / "investable_v3"
    / "tanker_delta"
    / "2026-08-21"
    / "transportation_tanker_semantic_replay_accepted.csv"
)
DEFAULT_OUTPUT_ROOT = ROOT / "investable_v5" / "fact_conflict_resolution_v3"

RESOLUTION_FIELDS = (
    "conflict_group_id",
    "conflict_resolution_status",
    "conflict_resolution_rule",
    "conflict_applied_rules",
    "original_value",
    "value_normalization",
)
GROUP_FIELDS = (
    "conflict_id",
    "ticker",
    "metric_id",
    "period_end",
    "available_on",
    "original_candidate_count",
    "original_distinct_value_count",
    "original_values_json",
    "retained_candidate_count",
    "retained_distinct_value_count",
    "retained_values_json",
    "resolution_status",
    "resolution_rule",
    "applied_rules",
    "residual_classification",
    "confirmed_true_contradiction_flag",
    "retained_candidate_keys",
    "suppressed_candidate_keys",
    "candidate_summary_json",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--surface-replay", type=Path, default=DEFAULT_SURFACE_REPLAY)
    parser.add_argument("--tanker-replay", type=Path, default=DEFAULT_TANKER_REPLAY)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def read_csv(path: Path, *, replay_lane: str) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or ())
        rows = []
        for raw in reader:
            row = dict(raw)
            row["_source_replay"] = replay_lane
            rows.append(row)
    if not fields:
        raise ValueError(f"{path}: accepted replay has no header")
    return fields, rows


def source_metric_ids(policy: Mapping[str, Any]) -> set[str]:
    return {
        str(metric_id)
        for cohort in policy["cohorts"].values()
        for group in cohort["groups"].values()
        for feature in (group.get("specialized_pack") or {}).values()
        for metric_id in (
            feature.get("source_metrics") or [feature.get("source_metric")]
        )
        if metric_id
    }


def read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_evidence(
    connection: sqlite3.Connection,
    evidence_keys: Iterable[str],
) -> dict[str, dict[str, object]]:
    keys = sorted({key for key in evidence_keys if key})
    output: dict[str, dict[str, object]] = {}
    for offset in range(0, len(keys), 500):
        batch = keys[offset : offset + 500]
        marks = ",".join("?" for _ in batch)
        query = (
            "SELECT * FROM sec_parser_metric_evidence_shadow "
            f"WHERE evidence_key IN ({marks})"
        )
        for row in connection.execute(query, batch):
            output[str(row["evidence_key"])] = dict(row)
    return output


def evidence_snapshot_sha256(
    evidence: Mapping[str, Mapping[str, object]],
) -> str:
    fields = (
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
    payload = [
        {field: evidence[key].get(field) for field in fields}
        for key in sorted(evidence)
    ]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_metadata(
    connection: sqlite3.Connection,
    run_ids: Iterable[int],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for run_id in sorted(set(run_ids)):
        row = connection.execute(
            "SELECT run_id, model_family, adapter_version, asof_date, mode, "
            "status, planned_work_count, completed_work_count, failed_work_count "
            "FROM sec_parser_run WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"parser run {run_id} is missing from the source database")
        item = dict(row)
        if (
            str(item.get("model_family")) != "transportation"
            or str(item.get("status")) != "COMPLETED"
            or int(item.get("failed_work_count") or 0) != 0
        ):
            raise ValueError(f"parser run {run_id} is not a completed zero-failure run")
        output.append(item)
    return output


def clean_row(row: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def ensure_new_artifacts(paths: Iterable[Path], *, allow_overwrite: bool) -> None:
    if allow_overwrite:
        return
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError(
            "fact-conflict diagnostics are sealed; choose a new --output-dir "
            f"or pass --allow-overwrite: {existing}"
        )


def main() -> int:
    args = parse_args()
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
    policy_path = args.policy.expanduser().resolve()
    surface_path = args.surface_replay.expanduser().resolve()
    tanker_path = args.tanker_replay.expanduser().resolve()
    for label, path in (
        ("database", db_path),
        ("policy", policy_path),
        ("surface replay", surface_path),
        ("tanker replay", tanker_path),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"missing {label}: {path}")

    surface_fields, surface_rows = read_csv(surface_path, replay_lane="surface")
    tanker_fields, tanker_rows = read_csv(tanker_path, replay_lane="tanker")
    if surface_fields != tanker_fields:
        raise ValueError("surface and tanker accepted replay schemas differ")
    all_rows = surface_rows + tanker_rows
    policy = load_subgroup_score_policy(policy_path)
    metric_ids = source_metric_ids(policy)

    connection = read_only_connection(db_path)
    try:
        evidence_keys = {
            str(row.get("evidence_key") or "")
            for row in all_rows
            if row.get("evidence_key")
        }
        evidence = load_evidence(connection, evidence_keys)
        parser_runs = run_metadata(
            connection,
            (
                int(row["run_id"])
                for row in all_rows
                if str(row.get("run_id") or "").isdigit()
            ),
        )
    finally:
        connection.close()

    missing_parser_evidence = sorted(evidence_keys - set(evidence))
    if missing_parser_evidence:
        raise ValueError(
            "accepted parser evidence is missing from the source database: "
            f"count={len(missing_parser_evidence)}"
        )

    result = resolve_accepted_fact_conflicts(
        rows=all_rows,
        evidence_by_key=evidence,
        metric_ids=metric_ids,
    )
    normalized = [dict(row) for row in result.normalized_rows]
    history = build_fact_history(normalized)
    after_counts = resolver_selection_conflict_counts(
        history,
        metric_ids=metric_ids,
    )
    after_total = sum(after_counts.values())
    expected_after = int(result.manifest["resolver_conflict_count_after"])
    if after_total != expected_after:
        raise RuntimeError(
            "normalized replay/resolver conflict mismatch: "
            f"module={expected_after} resolver={after_total}"
        )

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / args.asof
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    group_path = output_dir / "transportation_v8_fact_conflict_audit.csv"
    evidence_path = output_dir / "transportation_v8_fact_conflict_evidence.csv"
    surface_output = (
        output_dir / "transportation_surface_semantic_replay_conflict_normalized.csv"
    )
    tanker_output = (
        output_dir / "transportation_tanker_semantic_replay_conflict_normalized.csv"
    )
    manifest_path = output_dir / "transportation_v8_fact_conflict_audit.json"
    artifacts = (group_path, evidence_path, surface_output, tanker_output, manifest_path)
    ensure_new_artifacts(artifacts, allow_overwrite=args.allow_overwrite)

    normalized_fields = tuple(surface_fields) + RESOLUTION_FIELDS
    surface_normalized = [
        clean_row(row) for row in normalized if row.get("_source_replay") == "surface"
    ]
    tanker_normalized = [
        clean_row(row) for row in normalized if row.get("_source_replay") == "tanker"
    ]
    write_csv_atomic(group_path, GROUP_FIELDS, result.group_audit_rows)
    evidence_fields = tuple(result.evidence_audit_rows[0])
    write_csv_atomic(evidence_path, evidence_fields, result.evidence_audit_rows)
    write_csv_atomic(surface_output, normalized_fields, surface_normalized)
    write_csv_atomic(tanker_output, normalized_fields, tanker_normalized)

    manifest = dict(result.manifest)
    manifest.update(
        {
            "acceptance": "PASS",
            "asof_date": args.asof,
            "contract_version": POLICY_VERSION,
            "source_database": {
                "path": str(db_path),
                "read_only": True,
                "write_count": 0,
                "file_size": db_path.stat().st_size,
                "modified_ns": db_path.stat().st_mtime_ns,
            },
            "source_parser_runs": parser_runs,
            "source_evidence_row_count": len(evidence),
            "source_evidence_snapshot_sha256": evidence_snapshot_sha256(evidence),
            "lineage": {
                "policy": {
                    "path": str(policy_path),
                    "sha256": file_sha256(policy_path),
                },
                "surface_accepted_replay": {
                    "path": str(surface_path),
                    "sha256": file_sha256(surface_path),
                },
                "tanker_accepted_replay": {
                    "path": str(tanker_path),
                    "sha256": file_sha256(tanker_path),
                },
            },
            "resolver_conflict_count_after_by_metric_verified": after_counts,
            "artifacts": {
                "group_audit": {"path": str(group_path)},
                "evidence_audit": {"path": str(evidence_path)},
                "surface_normalized_replay": {"path": str(surface_output)},
                "tanker_normalized_replay": {"path": str(tanker_output)},
            },
            "historical_results_can_authorize_production": False,
            "production_activation_authorized": False,
            "next_gate": (
                "RERUN_V8_DIAGNOSTICS_WITH_VERSIONED_NORMALIZED_REPLAYS_"
                f"WHILE_{after_total}_RESIDUAL_IDENTITIES_REMAIN_FAIL_CLOSED"
            ),
        }
    )
    for item in manifest["artifacts"].values():
        path = Path(str(item["path"]))
        item["sha256"] = file_sha256(path)
    write_text_atomic(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
