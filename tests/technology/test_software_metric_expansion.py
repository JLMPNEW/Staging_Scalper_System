from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from dedicated_parser.contracts import stable_hash
from technology.software_infrastructure.software_metric_governance import (
    _row_payload,
)
from technology.software_infrastructure.software_metric_review import (
    build_review_rows,
    validate_review_rows,
)
from technology.software_infrastructure.software_nrr_discovery import (
    select_nrr_accessions,
)
from technology.software_infrastructure.software_parser_hydration import (
    select_filings,
)


def connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _source_row() -> dict[str, Any]:
    return {
        "evidence_key": "e1",
        "work_key": "w1",
        "model_family": "software_infrastructure",
        "adapter_version": "v1",
        "ticker": "TEST",
        "cik": "0000000001",
        "accession_number": "0000000001-24-000001",
        "form_type": "10-Q",
        "filing_date": "2024-05-01",
        "accepted_at": "2024-05-01T20:00:00Z",
        "report_date": "2024-03-31",
        "metric_name": "annual_recurring_revenue",
        "concept_name": "SoftwareDisclosureProseCandidate",
        "candidate_value": 100.0,
        "unit": "USD",
        "period_start": "",
        "period_end": "2024-03-31",
        "scope": "consolidated",
        "confidence": 0.7,
        "candidate_status": "REVIEW_REQUIRED",
        "status_reason": "review",
        "evidence_text": "ARR was $100 million.",
        "source_document": "ex991.htm",
        "extraction_method": "software_adapter:semantic_prose",
        "provenance_json": '{"document_sha256":"' + "a" * 64 + '"}',
        "parser_release": "0.4.6",
    }


def _queue_row(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "metric_family": "annual_recurring_revenue",
        "review_status": "PENDING_ADJUDICATION",
        "hard_negative_candidate_flag": "0",
        "historical_member_flag": "0",
        "membership_status_at_filing": "active",
        "ticker": source["ticker"],
        "accession_number": source["accession_number"],
        "form_type": source["form_type"],
        "accepted_at": source["accepted_at"],
        "source_document": source["source_document"],
        "source_metric": source["metric_name"],
        "candidate_value": str(source["candidate_value"]),
        "unit": source["unit"],
        "period_end": source["period_end"],
        "parser_status": source["candidate_status"],
        "parser_reason": source["status_reason"],
        "source_evidence_key": source["evidence_key"],
        "source_row_sha256": stable_hash(_row_payload(source)),
        "evidence_text": source["evidence_text"],
    }


def test_review_workbook_is_source_sealed_and_fail_closed() -> None:
    source = _source_row()
    queue = [_queue_row(source)]
    review = build_review_rows(
        queue,
        source_evidence={"e1": source},
    )
    errors, summary = validate_review_rows(
        review,
        queue_rows=queue,
        source_evidence={"e1": source},
    )
    assert errors == []
    assert summary["pending_review_count"] == 1
    assert summary["ready_for_release_flag"] == 0

    review[0].update(
        {
            "reviewer": "analyst",
            "reviewed_at_utc": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "decision": "ACCEPTED",
            "decision_reason": "confirmed total ARR",
            "effective_metric": "annual_recurring_revenue",
            "effective_value": "100",
            "effective_unit": "USD",
            "effective_period_start": "",
            "effective_period_end": "2024-03-31",
            "effective_scope": "consolidated",
            "period_kind": "instant",
            "definition_variant": "total_arr",
            "calibration_eligible_flag": "1",
        }
    )
    errors, summary = validate_review_rows(
        review,
        queue_rows=queue,
        source_evidence={"e1": source},
    )
    assert errors == []
    assert summary["ready_for_release_flag"] == 1

    review[0]["evidence_text"] = "tampered"
    errors, _summary = validate_review_rows(
        review,
        queue_rows=queue,
        source_evidence={"e1": source},
    )
    assert any("immutable field changed" in error for error in errors)
    assert any("review source seal mismatch" in error for error in errors)


def test_cross_family_metric_correction_requires_explicit_evidence_key() -> None:
    reviewed_key = (
        "d1f0b97e8da7194ee54718648a52ccf0254b4918171aeff3d1c5ffdb37dd4656"
    )
    source = _source_row()
    source.update(
        {
            "evidence_key": reviewed_key,
            "metric_name": "current_remaining_performance_obligation",
            "candidate_value": 786_700_000.0,
            "evidence_text": "Current contract liabilities were $767.2 million.",
        }
    )
    queue = _queue_row(source)
    queue["metric_family"] = "remaining_performance_obligation"
    review = build_review_rows(
        [queue],
        source_evidence={reviewed_key: source},
    )
    review[0].update(
        {
            "reviewer": "analyst",
            "reviewed_at_utc": "2026-07-30T12:00:00Z",
            "decision": "CORRECTED",
            "decision_reason": "contract liability is current deferred revenue",
            "effective_metric": "deferred_revenue_current",
            "effective_value": "767244000",
            "effective_unit": "USD",
            "effective_period_start": "",
            "effective_period_end": "2024-03-31",
            "effective_scope": "consolidated",
            "period_kind": "instant",
            "definition_variant": "current_deferred_revenue",
            "calibration_eligible_flag": "1",
        }
    )
    errors, summary = validate_review_rows(
        review,
        queue_rows=[queue],
        source_evidence={reviewed_key: source},
    )
    assert errors == []
    assert summary["ready_for_release_flag"] == 1

    unreviewed_source = dict(source)
    unreviewed_source["evidence_key"] = "unreviewed"
    unreviewed_queue = _queue_row(unreviewed_source)
    unreviewed_queue["metric_family"] = "remaining_performance_obligation"
    unreviewed_review = build_review_rows(
        [unreviewed_queue],
        source_evidence={"unreviewed": unreviewed_source},
    )
    decision_fields = {
        "reviewer",
        "reviewed_at_utc",
        "decision",
        "decision_reason",
        "effective_metric",
        "effective_value",
        "effective_unit",
        "effective_period_start",
        "effective_period_end",
        "effective_scope",
        "period_kind",
        "definition_variant",
        "calibration_eligible_flag",
    }
    unreviewed_review[0].update(
        {
            field: value
            for field, value in review[0].items()
            if field in decision_fields
        }
    )
    errors, _summary = validate_review_rows(
        unreviewed_review,
        queue_rows=[unreviewed_queue],
        source_evidence={"unreviewed": unreviewed_source},
    )
    assert any("invalid effective metric" in error for error in errors)


def test_hydration_selector_enforces_start_date_and_exact_accession() -> None:
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE dim_universe_membership(
                model_family TEXT,
                ticker TEXT,
                start_date TEXT,
                end_date TEXT
            );
            CREATE TABLE fact_sec_filing(
                ticker TEXT,
                cik TEXT,
                accession_number TEXT,
                form_type TEXT,
                filing_date TEXT,
                acceptance_datetime TEXT,
                report_date TEXT,
                primary_document TEXT,
                source_id TEXT
            );
            INSERT INTO dim_universe_membership
            VALUES ('software_infrastructure', 'TEST', '2020-01-01', '');
            INSERT INTO fact_sec_filing VALUES
              ('TEST', '1', 'old', '10-Q', '2023-11-01',
               '2023-11-01T20:00:00Z', '2023-09-30', 'old.htm', 'sec'),
              ('TEST', '1', 'target', '10-Q', '2024-05-01',
               '2024-05-01T20:00:00Z', '2024-03-31', 'target.htm', 'sec'),
              ('TEST', '1', 'other', '10-Q', '2024-08-01',
               '2024-08-01T20:00:00Z', '2024-06-30', 'other.htm', 'sec');
            """
        )
        selected = select_filings(
            conn,
            forms=("10-Q",),
            asof_date="2024-12-31",
            start_date="2024-01-01",
            accessions=("target",),
            max_filings_per_ticker=0,
        )
    assert [row.accession_number for row in selected] == ["target"]


def test_nrr_targeting_keeps_periodic_adjacent_event_and_one_registration() -> None:
    filings = [
        {
            "ticker": "TEST",
            "accession_number": "s1",
            "form_type": "S-1",
            "filing_date": "2023-01-01",
        },
        {
            "ticker": "TEST",
            "accession_number": "q1",
            "form_type": "10-Q",
            "filing_date": "2024-05-01",
        },
        {
            "ticker": "TEST",
            "accession_number": "e1",
            "form_type": "8-K",
            "filing_date": "2024-04-29",
        },
        {
            "ticker": "TEST",
            "accession_number": "e2",
            "form_type": "8-K",
            "filing_date": "2024-07-15",
        },
    ]
    selected = select_nrr_accessions(filings)
    assert {
        (row["form_type"], row["selection_tier"])
        for row in selected
    } == {
        ("S-1", "registration_baseline"),
        ("10-Q", "periodic"),
        ("8-K", "earnings_adjacent_event"),
    }
