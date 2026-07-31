#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dedicated_parser.contracts import file_sha256  # noqa: E402
from dedicated_parser.storage import connect_database  # noqa: E402
from industrials.core.config import (  # noqa: E402
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
    resolve_foundation,
)
from industrials.transportation.parser_coverage import (  # noqa: E402
    PARSER_DERIVATIONS,
)


EVIDENCE_FIELDS = (
    "queue_rank",
    "review_priority",
    "record_type",
    "run_id",
    "ticker",
    "universe_role",
    "calibration_cohort",
    "primary_archetype",
    "metric_id",
    "metric_pack",
    "source_lane",
    "coverage_status",
    "coverage_target_class",
    "minimum_usable_shortfall",
    "desired_action",
    "evidence_rank",
    "pair_evidence_count",
    "evidence_source_metric_id",
    "evidence_key",
    "candidate_status",
    "candidate_value",
    "unit",
    "period_start",
    "period_end",
    "scope",
    "confidence",
    "concept_name",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "report_date",
    "source_document",
    "source_path",
    "source_content_sha256",
    "extraction_method",
    "status_reason",
    "evidence_text",
    "provenance_json",
    "review_decision",
    "decision_reason",
    "review_notes",
    "reviewed_by",
    "reviewed_at",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Attach compact read-only evidence previews to the bounded "
            "transportation coverage-lift review queue."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--max-review-priority", type=int, default=3)
    parser.add_argument("--max-evidence-per-pair", type=int, default=5)
    return parser.parse_args(argv)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {
                str(key): str(value or "").strip()
                for key, value in row.items()
            }
            for row in csv.DictReader(handle)
        ]


def _chunks(
    values: list[tuple[str, str]],
    size: int,
) -> Iterable[list[tuple[str, str]]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _descending_date_key(value: object) -> int:
    digits = "".join(
        character
        for character in str(value or "")
        if character.isdigit()
    )
    return -int((digits[:8] or "0").ljust(8, "0"))


def _evidence_rows(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], list[dict[str, object]]]:
    output: dict[
        tuple[str, str],
        list[dict[str, object]],
    ] = defaultdict(list)
    for chunk in _chunks(pairs, 200):
        placeholders = ",".join("(?,?)" for _ in chunk)
        parameters: list[object] = [run_id]
        for ticker, metric_id in chunk:
            parameters.extend((ticker, metric_id))
        rows = connection.execute(
            f"""
            SELECT e.evidence_key, e.ticker, e.metric_name,
                   e.candidate_status, e.candidate_value, e.unit,
                   e.period_start, e.period_end, e.scope, e.confidence,
                   e.concept_name, e.accession_number, e.form_type,
                   e.filing_date, e.accepted_at, e.report_date,
                   e.source_document, e.extraction_method,
                   e.status_reason, e.evidence_text, e.provenance_json
            FROM sec_parser_run_metric_evidence AS relation
            JOIN sec_parser_metric_evidence_shadow AS e
              ON e.evidence_key=relation.evidence_key
            WHERE relation.run_id=?
              AND (e.ticker, e.metric_name) IN ({placeholders})
            """,
            tuple(parameters),
        ).fetchall()
        for row in rows:
            record = dict(row)
            output[
                (str(record["ticker"]), str(record["metric_name"]))
            ].append(record)
    status_order = {
        "ACCEPTED": 0,
        "REVIEW_REQUIRED": 1,
        "REJECTED_POLICY": 2,
        "SUPPRESSED_POLICY": 3,
        "PARSER_FAILURE": 4,
    }
    for rows in output.values():
        rows.sort(
            key=lambda row: (
                status_order.get(str(row["candidate_status"]), 9),
                -float(str(row["confidence"] or 0.0)),
                _descending_date_key(row["period_end"]),
                _descending_date_key(row["filing_date"]),
                str(row["evidence_key"]),
            ),
            reverse=False,
        )
    return output


def _source_map(
    rows: Iterable[Mapping[str, str]],
) -> dict[tuple[str, str, str], dict[str, str]]:
    return {
        (
            str(row.get("ticker") or ""),
            str(row.get("accession_number") or ""),
            str(row.get("document_name") or ""),
        ): {
            "source_path": str(row.get("local_path") or ""),
            "source_content_sha256": str(
                row.get("content_sha256") or ""
            ),
        }
        for row in rows
    }


def _derived_dependencies(path: Path) -> dict[str, tuple[str, ...]]:
    output: dict[str, list[str]] = defaultdict(list)
    for metric_id, contract in PARSER_DERIVATIONS.items():
        output[metric_id].extend(
            str(value) for value in contract["dependencies"]
        )
    for row in _read_csv(path):
        support = row["support_metric_id"]
        for consumer in row["consumer_metric_ids"].split("|"):
            if consumer:
                output[consumer].append(support)
    return {
        metric_id: tuple(sorted(set(sources)))
        for metric_id, sources in output.items()
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_review_priority < 1:
        raise ValueError("--max-review-priority must be at least 1")
    if args.max_evidence_per_pair < 1:
        raise ValueError("--max-evidence-per-pair must be at least 1")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    parser_cfg = family["dedicated_parser"]
    if bool(parser_cfg.get("parser_execution_authorized")):
        raise ValueError(
            "Evidence packaging requires parser_execution_authorized=false"
        )
    foundation = resolve_foundation(config_path, args.db)
    base_dir = config_path.parent
    asof_date = str(parser_cfg["source_census_asof_date"])
    output_dir = (
        resolve_path(parser_cfg["output_root"], base_dir=base_dir)
        / asof_date
    )
    lift_manifest_path = (
        output_dir / "transportation_coverage_lift_manifest.json"
    )
    queue_path = (
        output_dir / "transportation_coverage_lift_review_queue.csv"
    )
    census_path = resolve_path(
        parser_cfg["source_census_csv"],
        base_dir=base_dir,
    )
    support_registry_path = resolve_path(
        parser_cfg["supporting_registry_csv"],
        base_dir=base_dir,
    )
    for path in (
        lift_manifest_path,
        queue_path,
        census_path,
        support_registry_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    lift_manifest = json.loads(
        lift_manifest_path.read_text(encoding="utf-8")
    )
    if (
        lift_manifest.get("acceptance") != "PASS"
        or lift_manifest.get("gate")
        != "DP6A_BOUNDED_COVERAGE_LIFT_PACKAGE"
    ):
        raise ValueError("Evidence package requires a passing DP6A manifest")
    run_id = int(lift_manifest["run_id"])
    queue = [
        row
        for row in _read_csv(queue_path)
        if int(row["review_priority"]) <= args.max_review_priority
    ]
    dependencies = _derived_dependencies(support_registry_path)
    pairs = sorted(
        {
            (row["ticker"], source_metric)
            for row in queue
            for source_metric in dependencies.get(
                row["metric_id"],
                (row["metric_id"],),
            )
        }
    )
    with connect_database(
        foundation.db_path,
        timeout_seconds=foundation.timeout_sec,
        readonly=True,
    ) as connection:
        run = connection.execute(
            """
            SELECT model_family, status, failed_work_count
            FROM sec_parser_run
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        if (
            run is None
            or str(run["model_family"]) != MODEL_FAMILY
            or str(run["status"]) != "COMPLETED"
            or int(run["failed_work_count"]) != 0
        ):
            raise ValueError(
                f"run_id={run_id} is not a completed zero-failure run"
            )
        evidence = _evidence_rows(
            connection,
            run_id=run_id,
            pairs=pairs,
        )
    sources = _source_map(_read_csv(census_path))
    output: list[dict[str, object]] = []
    pairs_without_evidence = 0
    for pair in queue:
        source_metrics = dependencies.get(
            pair["metric_id"],
            (pair["metric_id"],),
        )
        rows = [
            evidence_row
            for source_metric in source_metrics
            for evidence_row in evidence.get(
                (pair["ticker"], source_metric),
                [],
            )
        ]
        rows.sort(
            key=lambda row: (
                0
                if str(row.get("candidate_status") or "")
                == "REVIEW_REQUIRED"
                else 1,
                -float(str(row.get("confidence") or 0.0)),
                _descending_date_key(row.get("period_end")),
                _descending_date_key(row.get("filing_date")),
                str(row.get("evidence_key") or ""),
            )
        )
        if not rows:
            pairs_without_evidence += 1
            rows = [{}]
        pair_count = len(rows) if rows and rows[0] else 0
        for rank, evidence_row in enumerate(
            rows[: args.max_evidence_per_pair],
            start=1,
        ):
            source = sources.get(
                (
                    pair["ticker"],
                    str(evidence_row.get("accession_number") or ""),
                    str(evidence_row.get("source_document") or ""),
                ),
                {},
            )
            output.append(
                {
                    "queue_rank": pair["queue_rank"],
                    "review_priority": pair["review_priority"],
                    "record_type": (
                        "EVIDENCE_PREVIEW"
                        if evidence_row
                        else "PAIR_WITHOUT_EVIDENCE"
                    ),
                    "run_id": run_id,
                    "ticker": pair["ticker"],
                    "universe_role": pair["universe_role"],
                    "calibration_cohort": pair["calibration_cohort"],
                    "primary_archetype": pair["primary_archetype"],
                    "metric_id": pair["metric_id"],
                    "metric_pack": pair["metric_pack"],
                    "source_lane": pair["source_lane"],
                    "coverage_status": pair["coverage_status"],
                    "coverage_target_class": pair[
                        "coverage_target_class"
                    ],
                    "minimum_usable_shortfall": pair[
                        "minimum_usable_shortfall"
                    ],
                    "desired_action": pair["desired_action"],
                    "evidence_rank": rank if evidence_row else 0,
                    "pair_evidence_count": pair_count,
                    "evidence_source_metric_id": evidence_row.get(
                        "metric_name",
                        "",
                    ),
                    "evidence_key": evidence_row.get(
                        "evidence_key",
                        "",
                    ),
                    "candidate_status": evidence_row.get(
                        "candidate_status",
                        "",
                    ),
                    "candidate_value": evidence_row.get(
                        "candidate_value",
                        "",
                    ),
                    "unit": evidence_row.get("unit", ""),
                    "period_start": evidence_row.get(
                        "period_start",
                        "",
                    ),
                    "period_end": evidence_row.get("period_end", ""),
                    "scope": evidence_row.get("scope", ""),
                    "confidence": evidence_row.get("confidence", ""),
                    "concept_name": evidence_row.get(
                        "concept_name",
                        "",
                    ),
                    "accession_number": evidence_row.get(
                        "accession_number",
                        "",
                    ),
                    "form_type": evidence_row.get("form_type", ""),
                    "filing_date": evidence_row.get("filing_date", ""),
                    "accepted_at": evidence_row.get("accepted_at", ""),
                    "report_date": evidence_row.get("report_date", ""),
                    "source_document": evidence_row.get(
                        "source_document",
                        "",
                    ),
                    "source_path": source.get("source_path", ""),
                    "source_content_sha256": source.get(
                        "source_content_sha256",
                        "",
                    ),
                    "extraction_method": evidence_row.get(
                        "extraction_method",
                        "",
                    ),
                    "status_reason": evidence_row.get(
                        "status_reason",
                        "",
                    ),
                    "evidence_text": evidence_row.get(
                        "evidence_text",
                        "",
                    ),
                    "provenance_json": evidence_row.get(
                        "provenance_json",
                        "",
                    ),
                    "review_decision": "",
                    "decision_reason": "",
                    "review_notes": "",
                    "reviewed_by": "",
                    "reviewed_at": "",
                }
            )
    output_path = (
        output_dir
        / "transportation_coverage_lift_evidence_review.csv"
    )
    summary_path = (
        output_dir
        / "transportation_coverage_lift_evidence_manifest.json"
    )
    write_csv_atomic(output_path, EVIDENCE_FIELDS, output)
    record_counts = Counter(str(row["record_type"]) for row in output)
    candidate_status_counts = Counter(
        str(row["candidate_status"])
        for row in output
        if str(row["candidate_status"])
    )
    payload = {
        "acceptance": "PASS",
        "gate": "DP6B_COMPACT_EVIDENCE_REVIEW_PACKAGE",
        "model_family": MODEL_FAMILY,
        "asof_date": asof_date,
        "run_id": run_id,
        "max_review_priority": args.max_review_priority,
        "max_evidence_per_pair": args.max_evidence_per_pair,
        "selected_queue_pair_count": len(queue),
        "selected_source_pair_count": len(pairs),
        "pairs_without_evidence": pairs_without_evidence,
        "output_row_count": len(output),
        "record_type_counts": dict(sorted(record_counts.items())),
        "candidate_status_counts": dict(
            sorted(candidate_status_counts.items())
        ),
        "network_invocations": 0,
        "provider_invocations": 0,
        "parser_invocations": 0,
        "database_writes": 0,
        "feature_build_invocations": 0,
        "calibration_invocations": 0,
        "inputs": {
            "coverage_lift_manifest": {
                "path": str(lift_manifest_path),
                "sha256": file_sha256(lift_manifest_path),
            },
            "review_queue": {
                "path": str(queue_path),
                "sha256": file_sha256(queue_path),
            },
            "source_census": {
                "path": str(census_path),
                "sha256": file_sha256(census_path),
            },
        },
        "artifact": {
            "path": str(output_path),
            "row_count": len(output),
            "sha256": file_sha256(output_path),
        },
        "next_gate": "MANUAL_EVIDENCE_AND_SOURCE_CANDIDATE_REVIEW",
    }
    write_text_atomic(
        summary_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
