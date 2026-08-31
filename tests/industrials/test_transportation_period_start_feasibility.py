from __future__ import annotations

from industrials.transportation.period_start_feasibility import (
    classify_candidate_period_start,
    classify_conflict_group,
)


def candidate(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "period_start": "",
        "evidence_text": "",
        "provenance_json": "{}",
        "semantic_table_id": "",
        "semantic_block_index": "",
        "semantic_row_index": "",
    }
    row.update(updates)
    return row


def test_exact_bound_evidence_start_is_recoverable_without_inference() -> None:
    result = classify_candidate_period_start(
        candidate(),
        bound_evidence_period_start="2025-01-01",
    )
    assert result["exact_recoverable_flag"] == 1
    assert result["effective_period_start"] == "2025-01-01"
    assert result["recovery_reason"] == "EXACT_BOUND_EVIDENCE_PERIOD_START"


def test_duration_and_table_context_are_not_converted_into_a_start_date() -> None:
    result = classify_candidate_period_start(
        candidate(
            evidence_text="For the three months ended March 31, 2025",
            semantic_table_id="14",
        )
    )
    assert result["duration_phrase_flag"] == 1
    assert result["semantic_table_locator_flag"] == 1
    assert result["exact_recoverable_flag"] == 0
    assert result["effective_period_start"] == ""
    assert result["recovery_reason"] == (
        "DURATION_ONLY_CONTEXT_REQUIRES_CALENDAR_INFERENCE"
    )


def test_unlinked_explicit_date_range_requires_metric_adjudication() -> None:
    result = classify_candidate_period_start(
        candidate(
            evidence_text=(
                "Operations were disrupted from February 8, 2020 through "
                "June 8, 2020."
            )
        )
    )
    assert result["explicit_full_date_range_flag"] == 1
    assert result["exact_recoverable_flag"] == 0
    assert result["recovery_reason"] == (
        "EXPLICIT_DATE_RANGE_REQUIRES_METRIC_LINK_ADJUDICATION"
    )


def test_group_requires_every_candidate_to_share_one_exact_bound_start() -> None:
    known = {"effective_period_start": "2025-01-01", "exact_recoverable_flag": 0}
    missing = {"effective_period_start": "", "exact_recoverable_flag": 0}
    mixed = classify_conflict_group([known, missing])
    assert mixed["group_exact_recoverable_flag"] == 0
    assert mixed["feasibility_category"] == (
        "MISSING_WITH_ONE_KNOWN_ANCHOR_NO_EXACT_LINK"
    )

    recovered = {
        "effective_period_start": "2025-01-01",
        "exact_recoverable_flag": 1,
    }
    exact = classify_conflict_group([known, recovered])
    assert exact["group_exact_recoverable_flag"] == 1
    assert exact["exact_recovered_period_start"] == "2025-01-01"


def test_complete_but_different_period_starts_remain_conflicted() -> None:
    result = classify_conflict_group(
        [
            {"effective_period_start": "2025-01-01"},
            {"effective_period_start": "2024-10-01"},
        ]
    )
    assert result["group_exact_recoverable_flag"] == 0
    assert result["feasibility_category"] == (
        "COMPLETE_CONFLICTING_PERIOD_STARTS"
    )
