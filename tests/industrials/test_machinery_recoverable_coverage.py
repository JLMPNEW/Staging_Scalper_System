from __future__ import annotations

import sqlite3

from industrials.core.db import init_db
from industrials.machinery.recoverable_coverage import (
    classify_recovery,
    is_current_candidate,
    is_recent_public,
    parse_missing_operands,
)


def classify(**overrides: object):
    inputs: dict[str, object] = {
        "metric_name": "reported_backlog",
        "availability_status": "NOT_DISCLOSED",
        "missing_operands": (),
        "source_metric": "reported_backlog",
        "source_status": "NOT_DISCLOSED",
        "accepted_candidate_count": 0,
        "review_candidate_count": 0,
        "rejected_candidate_count": 0,
        "unmapped_matching_fact_count": 0,
        "registration_filing_count": 0,
        "recent_public": False,
    }
    inputs.update(overrides)
    return classify_recovery(**inputs)  # type: ignore[arg-type]


def test_parse_missing_operands() -> None:
    assert parse_missing_operands(
        "insufficient_comparable_history_or_missing_operands:orders,prior_comparable_orders"
    ) == ("orders", "prior_comparable_orders")
    assert parse_missing_operands("issuer_did_not_report_metric") == ()


def test_recent_public_is_relative_to_asof() -> None:
    assert is_recent_public(membership_start="2025-04-15", asof="2026-07-20")
    assert not is_recent_public(membership_start="2023-04-15", asof="2026-07-20")
    assert not is_recent_public(membership_start="2026-07-21", asof="2026-07-20")


def test_current_candidate_uses_feature_period_not_any_historical_match() -> None:
    assert is_current_candidate(
        {"period_end": "2026-03-31", "filing_date": "2026-05-01"},
        anchor_period="2025-12-31",
        asof="2026-07-20",
    )
    assert not is_current_candidate(
        {"period_end": "2023-12-31", "filing_date": "2024-02-01"},
        anchor_period="2025-12-31",
        asof="2026-07-20",
    )


def test_projection_and_review_evidence_are_high_priority() -> None:
    assert classify(accepted_candidate_count=1).evidence_class == "ACCEPTED_FACT_NOT_PROJECTED"
    review = classify(review_candidate_count=2)
    assert (review.evidence_class, review.recoverability) == (
        "DISCLOSED_REQUIRES_REVIEW",
        "HIGH",
    )


def test_unmapped_custom_concept_is_high_priority() -> None:
    result = classify(unmapped_matching_fact_count=8)
    assert (result.evidence_class, result.source_lane) == (
        "UNMAPPED_XBRL_CONCEPT",
        "CUSTOM_XBRL",
    )


def test_missing_prior_history_routes_to_registration_statement() -> None:
    result = classify(
        metric_name="rpo_yoy_growth",
        missing_operands=("remaining_performance_obligation", "prior_comparable_rpo"),
        source_metric="remaining_performance_obligation",
        source_status="REPORTED",
        registration_filing_count=2,
    )
    assert (result.evidence_class, result.source_lane) == (
        "INSUFFICIENT_COMPARABLE_HISTORY",
        "REGISTRATION_STATEMENT",
    )


def test_covered_source_with_other_missing_operand_routes_to_alignment() -> None:
    result = classify(
        metric_name="reported_backlog_to_revenue",
        missing_operands=("reported_backlog", "revenue_ttm"),
        source_metric="reported_backlog",
        source_status="REPORTED",
        accepted_candidate_count=2,
    )
    assert (result.evidence_class, result.source_lane) == (
        "DERIVATION_ALIGNMENT_GAP",
        "PIPELINE_ALIGNMENT",
    )


def test_current_rpo_routes_to_text_disaggregation() -> None:
    result = classify(
        metric_name="rpo_current",
        source_metric="remaining_performance_obligation",
    )
    assert result.evidence_class == "CURRENT_RPO_TEXT_DISAGGREGATION_NEEDED"


def test_full_history_recovery_indexes_are_part_of_schema() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {
        "idx_fact_sec_metric_disclosure_candidate_document",
        "idx_fact_sec_xbrl_fact_raw_document",
        "idx_fact_sec_xbrl_fact_raw_id",
    }.issubset(indexes)
