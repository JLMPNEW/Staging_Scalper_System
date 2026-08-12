from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from dedicated_parser.atomic_io import atomic_text_writer
from typing import Any, Iterable

from dedicated_parser.policy import POLICY_FIELDS


SKELETON_FIELDS = (
    *POLICY_FIELDS,
    "evidence_key",
    "recovery_class",
    "suggested_action",
    "evidence_text",
)


def build_adjudication_skeleton(
    conn: sqlite3.Connection,
    *,
    run_id: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT evidence.*, assessment.recovery_class
        FROM sec_parser_run_metric_evidence AS relation
        JOIN sec_parser_metric_evidence_shadow AS evidence
          ON evidence.evidence_key = relation.evidence_key
        LEFT JOIN sec_parser_recovery_assessment AS assessment
          ON assessment.run_id = relation.run_id
         AND assessment.model_family = evidence.model_family
         AND assessment.ticker = evidence.ticker
         AND assessment.metric_name = evidence.metric_name
        WHERE relation.run_id = ?
          AND evidence.candidate_status IN (
                'REVIEW_REQUIRED',
                'STRUCTURAL_NA'
          )
        ORDER BY evidence.ticker, evidence.metric_name,
                 evidence.period_end, evidence.accession_number,
                 evidence.evidence_key
        """,
        (run_id,),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        value = row["candidate_value"]
        tolerance = (
            max(1e-6, abs(float(value)) * 1e-12)
            if value is not None
            else 0.0
        )
        structural_candidate = value is None
        output.append(
            {
                "policy_id": (
                    f"review_{str(row['ticker']).lower()}_"
                    f"{str(row['metric_name']).lower()}_"
                    f"{str(row['evidence_key'])[:12]}"
                ),
                "policy_version": "1.0.0",
                "enabled": "false",
                "model_family": str(row["model_family"]),
                "ticker": str(row["ticker"]),
                "accession_number": str(row["accession_number"]),
                "source_document": str(row["source_document"]),
                "metric_name": str(row["metric_name"]),
                "concept_name": str(row["concept_name"]),
                "candidate_value": "" if value is None else value,
                "value_tolerance": tolerance,
                "unit": str(row["unit"] or ""),
                "period_start": str(row["period_start"] or ""),
                "period_end": str(row["period_end"] or ""),
                "decision": (
                    "STRUCTURAL_NA"
                    if structural_candidate
                    and str(row["status_reason"])
                    != "confirmed_non_disclosure_not_structural_na"
                    else "REVIEW_REQUIRED"
                ),
                "status_reason": str(row["status_reason"] or ""),
                "scope_override": "",
                "confidence_override": "",
                "reviewed_by": "",
                "reviewed_at": "",
                "period_start_override": "",
                "period_end_override": "",
                "value_override": "",
                "evidence_key": str(row["evidence_key"]),
                "recovery_class": str(row["recovery_class"] or ""),
                "suggested_action": (
                    "verify_structural_applicability"
                    if structural_candidate
                    else "verify_value_period_unit_scope"
                ),
                "evidence_text": str(row["evidence_text"] or ""),
            }
        )
    return output


def write_adjudication_skeleton(
    path: Path,
    rows: Iterable[dict[str, Any]],
) -> None:
    with atomic_text_writer(path, newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=SKELETON_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in SKELETON_FIELDS})
