from __future__ import annotations

import pytest

from industrials.transportation.semantic_replay_contract import (
    resolve_semantic_replay_rows,
)
from industrials.transportation.semantic_candidate_materialization import (
    build_materialization_candidates,
)


def _row(key: str, value: str, **updates: str) -> dict[str, str]:
    row = {
        "candidate_key": key,
        "ticker": "JBHT",
        "metric_id": "average_length_of_haul",
        "value": value,
        "unit": "distance",
        "period_end": "2025-12-31",
        "filing_date": "2026-02-24",
        "accession_number": "0000000000-26-000001",
        "replay_status": "ACCEPTED",
    }
    row.update(updates)
    return row


def test_identical_duplicate_values_collapse_deterministically() -> None:
    result = resolve_semantic_replay_rows(
        [_row("b", "415"), _row("a", "415.0")]
    )
    assert result.conflict_group_count == 0
    assert len(result.conflict_free_rows) == 1
    assert result.conflict_free_rows[0]["candidate_key"] == "a"


def test_different_values_in_same_filing_period_are_fail_closed() -> None:
    result = resolve_semantic_replay_rows(
        [_row("a", "169"), _row("b", "1679")]
    )
    assert result.conflict_group_count == 1
    assert result.conflict_free_rows == ()
    assert {row["conflict_reason"] for row in result.conflict_rows} == {
        "same_filing_period_metric_has_multiple_values"
    }


def test_different_filings_remain_distinct_point_in_time_observations() -> None:
    result = resolve_semantic_replay_rows(
        [
            _row("a", "415", filing_date="2026-02-24"),
            _row(
                "b",
                "420",
                filing_date="2026-05-01",
                accession_number="0000000000-26-000002",
            ),
        ]
    )
    assert result.conflict_group_count == 0
    assert len(result.conflict_free_rows) == 2


def test_incomplete_identity_is_not_materializable() -> None:
    result = resolve_semantic_replay_rows([_row("a", "415", period_end="")])
    assert result.conflict_group_count == 1
    assert result.conflict_free_rows == ()
    assert result.conflict_rows[0]["conflict_reason"] == (
        "invalid_or_incomplete_observation_identity"
    )


def _reviewed_row(**updates: str) -> dict[str, str]:
    row = {
        "definition_id": "definition",
        "candidate_key": "raw-candidate",
        "source_lane": "parser_run_evidence",
        "ticker": "ARCB",
        "metric_id": "operating_ratio",
        "value": "0.91",
        "unit": "ratio",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "filing_date": "2026-02-20",
        "accepted_at": "2026-02-20T12:00:00Z",
        "form_type": "10-K",
        "accession_number": "0000000000-26-000001",
        "concept_name": "ReportedOperatingRatio",
        "source_document": "arcb-20251231x10k.htm",
        "source_content_sha256": "a" * 64,
        "evidence_key": "evidence",
        "replay_status": "ACCEPTED",
        "replay_reason": "approved",
        "review_policy_version": "review-v1",
        "reviewed_by": "reviewer",
        "reviewed_at": "2026-08-14T23:00:00Z",
    }
    row.update(updates)
    return row


def test_semantic_materialization_requires_coverage_allowlist_and_preserves_zero() -> None:
    rows = [_reviewed_row(value="0"), _reviewed_row(ticker="XPO", evidence_key="xpo")]
    materialized = build_materialization_candidates(
        rows,
        lane="surface",
        allowed_pairs={("ARCB", "operating_ratio")},
        asof="2026-08-13",
        lineage={"manifest_sha256": "b" * 64},
    )
    assert len(materialized) == 1
    assert materialized[0].ticker == "ARCB"
    assert materialized[0].value == 0.0
    assert "manifest_sha256" in materialized[0].provenance_json


def test_semantic_materialization_rejects_nonaccepted_or_bad_hash() -> None:
    with pytest.raises(ValueError, match="not ACCEPTED"):
        build_materialization_candidates(
            [_reviewed_row(replay_status="REVIEW_REQUIRED")],
            lane="surface",
            allowed_pairs={("ARCB", "operating_ratio")},
            asof="2026-08-13",
            lineage={},
        )
    with pytest.raises(ValueError, match="invalid source content hash"):
        build_materialization_candidates(
            [_reviewed_row(source_content_sha256="bad")],
            lane="surface",
            allowed_pairs={("ARCB", "operating_ratio")},
            asof="2026-08-13",
            lineage={},
        )


def test_semantic_materialization_accepts_formula_locked_fact_store_ratio() -> None:
    row = _reviewed_row(
        source_lane="fact_store_ratio",
        source_document="sec_companyfacts",
        source_content_sha256="",
        evidence_key="",
        formula="numerator/denominator",
        numerator_concept="OperatingExpenses",
        denominator_concept="Revenue",
    )
    materialized = build_materialization_candidates(
        [row],
        lane="surface",
        allowed_pairs={("ARCB", "operating_ratio")},
        asof="2026-08-13",
        lineage={},
    )
    assert len(materialized) == 1
    assert materialized[0].evidence_key == "raw-candidate"
