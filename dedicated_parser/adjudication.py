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

AMBIGUOUS_REVIEW_FIELDS = (
    *POLICY_FIELDS,
    'evidence_key',
    'base_candidate_status',
    'base_status_reason',
    'recovery_class',
    'predicted_status',
    'accepted_current_count',
    'accepted_historical_count',
    'review_required_count',
    'rejected_count',
    'parser_failure_count',
    'searched_filing_count',
    'searched_document_count',
    'work_key',
    'cik',
    'form_type',
    'filing_date',
    'accepted_at',
    'report_date',
    'provenance_json',
    'evidence_text',
    'suggested_action',
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


def build_ambiguous_adjudication_skeleton(
    conn: sqlite3.Connection,
    *,
    run_id: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT evidence.*,
               assessment.recovery_class,
               assessment.predicted_status,
               assessment.accepted_current_count,
               assessment.accepted_historical_count,
               assessment.review_required_count,
               assessment.rejected_count,
               assessment.parser_failure_count,
               assessment.searched_filing_count,
               assessment.searched_document_count
        FROM sec_parser_recovery_assessment AS assessment
        JOIN sec_parser_run_metric_evidence AS relation
          ON relation.run_id=assessment.run_id
        JOIN sec_parser_metric_evidence_shadow AS evidence
          ON evidence.evidence_key=relation.evidence_key
         AND evidence.model_family=assessment.model_family
         AND evidence.ticker=assessment.ticker
         AND evidence.metric_name=assessment.metric_name
        WHERE assessment.run_id=?
          AND assessment.recovery_class='FOUND_AMBIGUOUS'
          AND evidence.candidate_status IN (
              'ACCEPTED','REVIEW_REQUIRED','REJECTED_POLICY',
              'SUPPRESSED_SEMANTIC_DUPLICATE','STRUCTURAL_NA'
          )
        ORDER BY evidence.ticker,evidence.metric_name,
                 evidence.period_end DESC,evidence.accepted_at DESC,
                 evidence.accession_number,evidence.source_document,
                 evidence.evidence_key
        ''',
        (run_id,),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        output.append(_evidence_review_row(
            row,
            policy_prefix='ambiguous',
            suggested_action=(
                'select_exact_value_period_unit_scope_then_accept_or_reject'
            ),
        ))
    return output


def _evidence_review_row(
    row: sqlite3.Row,
    *,
    policy_prefix: str,
    suggested_action: str,
) -> dict[str, Any]:
    value = row['candidate_value']
    tolerance = (
        max(1e-6, abs(float(value)) * 1e-12)
        if value is not None
        else 0.0
    )
    candidate_status = str(row['candidate_status'])
    return {
            'policy_id': (
                f"{policy_prefix}_{str(row['ticker']).lower()}_"
                f"{str(row['metric_name']).lower()}_"
                f"{str(row['evidence_key'])[:12]}"
            ),
            'policy_version': '1.0.0',
            'enabled': 'false',
            'model_family': str(row['model_family']),
            'ticker': str(row['ticker']),
            'accession_number': str(row['accession_number']),
            'source_document': str(row['source_document']),
            'metric_name': str(row['metric_name']),
            'concept_name': str(row['concept_name']),
            'candidate_value': '' if value is None else value,
            'value_tolerance': tolerance,
            'unit': str(row['unit'] or ''),
            'period_start': str(row['period_start'] or ''),
            'period_end': str(row['period_end'] or ''),
            'decision': candidate_status,
            'status_reason': str(row['status_reason'] or ''),
            'scope_override': '',
            'confidence_override': '',
            'reviewed_by': '',
            'reviewed_at': '',
            'period_start_override': '',
            'period_end_override': '',
            'value_override': '',
            'evidence_key': str(row['evidence_key']),
            'base_candidate_status': candidate_status,
            'base_status_reason': str(row['status_reason'] or ''),
            'recovery_class': str(row['recovery_class']),
            'predicted_status': str(row['predicted_status']),
            'accepted_current_count': int(row['accepted_current_count']),
            'accepted_historical_count': int(
                row['accepted_historical_count']
            ),
            'review_required_count': int(row['review_required_count']),
            'rejected_count': int(row['rejected_count']),
            'parser_failure_count': int(row['parser_failure_count']),
            'searched_filing_count': int(row['searched_filing_count']),
            'searched_document_count': int(row['searched_document_count']),
            'work_key': str(row['work_key']),
            'cik': str(row['cik']),
            'form_type': str(row['form_type']),
            'filing_date': str(row['filing_date'] or ''),
            'accepted_at': str(row['accepted_at'] or ''),
            'report_date': str(row['report_date'] or ''),
            'provenance_json': str(row['provenance_json'] or '{}'),
            'evidence_text': str(row['evidence_text'] or ''),
            'suggested_action': suggested_action,
        }


def build_ocr_adjudication_skeleton(
    conn: sqlite3.Connection,
    *,
    run_id: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT evidence.*,
               assessment.recovery_class,
               assessment.predicted_status,
               assessment.accepted_current_count,
               assessment.accepted_historical_count,
               assessment.review_required_count,
               assessment.rejected_count,
               assessment.parser_failure_count,
               assessment.searched_filing_count,
               assessment.searched_document_count
        FROM sec_parser_run_metric_evidence AS relation
        JOIN sec_parser_metric_evidence_shadow AS evidence
          ON evidence.evidence_key=relation.evidence_key
        JOIN sec_parser_recovery_assessment AS assessment
          ON assessment.run_id=relation.run_id
         AND assessment.model_family=evidence.model_family
         AND assessment.ticker=evidence.ticker
         AND assessment.metric_name=evidence.metric_name
        WHERE relation.run_id=?
          AND evidence.candidate_status='REVIEW_REQUIRED'
          AND COALESCE(
              json_extract(evidence.provenance_json,'$.ocr_used'),0
          )=1
        ORDER BY evidence.ticker,evidence.metric_name,
                 evidence.period_end DESC,evidence.accepted_at DESC,
                 evidence.accession_number,evidence.source_document,
                 evidence.evidence_key
        ''',
        (run_id,),
    ).fetchall()
    return [
        _evidence_review_row(
            row,
            policy_prefix='ocr',
            suggested_action=(
                'compare_rendered_page_and_ocr_text_then_accept_or_reject'
            ),
        )
        for row in rows
    ]


def write_ambiguous_adjudication_skeleton(
    path: Path,
    rows: Iterable[dict[str, Any]],
) -> None:
    with atomic_text_writer(path, newline='') as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=AMBIGUOUS_REVIEW_FIELDS,
            extrasaction='ignore',
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({
                field: row.get(field, '') for field in AMBIGUOUS_REVIEW_FIELDS
            })
