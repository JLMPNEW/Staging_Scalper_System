from __future__ import annotations

from technology.software_infrastructure.software_arr_census_adjudication import (
    apply_arr_review_overrides,
    build_arr_proposals,
    propose_arr_candidate,
    summarize_arr_proposals,
)


def _row(
    *,
    key: str,
    accession: str,
    value: float,
    text: str,
    scope: str = "unknown",
) -> dict[str, object]:
    return {
        "evidence_key": key,
        "ticker": "TEST",
        "accession_number": accession,
        "accepted_at": "2026-05-01T20:00:00Z",
        "candidate_value": value,
        "unit": "USD",
        "period_end": "2026-03-31",
        "scope": scope,
        "evidence_text": text,
        "source_document": f"{key}.htm",
    }


def test_proposal_accepts_total_and_rejects_noncomparable_arr() -> None:
    total = propose_arr_candidate(
        _row(
            key="total",
            accession="a1",
            value=1_440_000_000.0,
            text="Total ARR grew to $1.440 billion at December 31, 2025.",
        )
    )
    customer = propose_arr_candidate(
        _row(
            key="customer",
            accession="a2",
            value=100_000.0,
            text="Customers with ARR of $100,000 or more grew 17%.",
        )
    )
    subset = propose_arr_candidate(
        _row(
            key="subset",
            accession="a3",
            value=218_000_000.0,
            text="Secure Communications ARR increased to $218 million.",
        )
    )
    guidance = propose_arr_candidate(
        _row(
            key="guidance",
            accession="a4",
            value=5_792.6,
            text="Annual recurring revenue $5,792.6 - $5,794.6 million.",
        )
    )
    adjacent_metric = propose_arr_candidate(
        _row(
            key="adjacent",
            accession="a5",
            value=220_000_000.0,
            text=("The company crossed $1 billion in ARR and delivered $220 million of free cash flow."),
        )
    )
    corrected_total = propose_arr_candidate(
        _row(
            key="corrected-total",
            accession="a6",
            value=289_500_000.0,
            text=("Subscription ARR was $289.5 million and Total ARR was $724.1 million."),
        )
    )
    total_with_later_subset = propose_arr_candidate(
        _row(
            key="total-with-later-subset",
            accession="a7",
            value=237_600_000.0,
            text=(
                "ARR was $237.6 million, up 22%. The portion of ARR related to cloud subscriptions was $101 million."
            ),
        )
    )
    assert total["proposal_decision"] == "CORRECTED"
    assert total["calibration_eligible_flag"] == 1
    assert customer["proposal_decision"] == "REJECTED_POLICY"
    assert subset["proposal_decision"] == "REJECTED_POLICY"
    assert guidance["proposal_decision"] == "REJECTED_POLICY"
    assert adjacent_metric["proposal_decision"] == "REJECTED_POLICY"
    assert corrected_total["proposal_decision"] == "CORRECTED"
    assert corrected_total["effective_value"] == 724_100_000.0
    assert total_with_later_subset["proposal_decision"] == "CORRECTED"


def test_proposals_select_one_canonical_value_per_accession() -> None:
    rows = [
        _row(
            key="rounded",
            accession="a1",
            value=1_440_000_000.0,
            text="Total ARR grew to $1.440 billion.",
        ),
        _row(
            key="exact",
            accession="a1",
            value=1_439_900_000.0,
            text="Total ARR was $1.4399 billion.",
            scope="consolidated",
        ),
    ]
    proposals = build_arr_proposals(rows)
    assert sum(int(row["canonical_candidate_flag"]) for row in proposals) == 1
    assert sum(int(row["proposal_decision"] == "REJECTED_POLICY") for row in proposals) == 1


def test_summary_fails_closed_while_review_rows_remain() -> None:
    accepted = propose_arr_candidate(
        _row(
            key="accepted",
            accession="a1",
            value=500_000_000.0,
            text="ARR was $500 million at quarter end.",
        )
    )
    accepted["canonical_candidate_flag"] = 1
    review = propose_arr_candidate(
        _row(
            key="review",
            accession="a2",
            value=123.0,
            text="ARR and other financial results were discussed.",
        )
    )
    _, summary = summarize_arr_proposals(
        [accepted, review],
        minimum_cross_section=1,
    )
    assert summary["strict_level_ticker_count"] == 1
    assert summary["human_approval_required_flag"] == 1
    assert summary["unresolved_review_required_flag"] == 1
    assert summary["historical_hydration_authorized_flag"] == 0


def test_total_arr_selector_ignores_trailing_percent_of_total_subset() -> None:
    proposal = propose_arr_candidate(
        _row(
            key="zixi-pattern",
            accession="a8",
            value=237_700_000.0,
            text=("ARR increased 14% to $237.7 million. Cloud ARR increased to $206.6 million or 87% of total ARR."),
        )
    )
    assert proposal["proposal_decision"] == "CORRECTED"
    assert proposal["effective_value"] == 237_700_000.0


def test_proposal_rejects_goal_and_lower_bound_guidance() -> None:
    goal = propose_arr_candidate(
        _row(
            key="goal",
            accession="a9",
            value=20_000_000_000.0,
            text="We raised our outlook and target a goal of $20 billion ARR.",
        )
    )
    lower_bound = propose_arr_candidate(
        _row(
            key="lower-bound",
            accession="a10",
            value=300_000_000.0,
            text="Total ARR $300 million or greater.",
        )
    )
    assert goal["proposal_decision"] == "REJECTED_POLICY"
    assert lower_bound["proposal_decision"] == "REJECTED_POLICY"


def test_review_overrides_reject_and_correct_with_source_assertions() -> None:
    reject = propose_arr_candidate(
        _row(
            key="reject",
            accession="a1",
            value=300_000_000.0,
            text="Total ARR was $300 million.",
            scope="consolidated",
        )
    )
    reject["canonical_candidate_flag"] = 0
    replacement = propose_arr_candidate(
        _row(
            key="unreviewed-replacement",
            accession="a1",
            value=290_000_000.0,
            text="Total ARR was $290 million.",
            scope="consolidated",
        )
    )
    replacement["canonical_candidate_flag"] = 1
    period = propose_arr_candidate(
        _row(
            key="period",
            accession="a2",
            value=1_032_000_000.0,
            text="ARR ended the quarter at $1,032 million.",
        )
    )
    period["canonical_candidate_flag"] = 1
    rows, summary = apply_arr_review_overrides(
        [reject, replacement, period],
        [
            {
                "action": "REJECT",
                "suppress_accession_reselection_flag": "1",
                "ticker": "TEST",
                "accession_number": "a1",
                "expected_candidate_value": "300000000",
                "corrected_effective_value": "",
                "expected_source_period_end": "2026-03-31",
                "corrected_effective_period_end": "",
                "issue": "guidance",
                "detail": "Not actual ARR.",
            },
            {
                "action": "FIX_PERIOD",
                "ticker": "TEST",
                "accession_number": "a2",
                "expected_candidate_value": "1032000000",
                "corrected_effective_value": "",
                "expected_source_period_end": "2026-03-31",
                "corrected_effective_period_end": "2026-02-28",
                "issue": "filing_date_used_as_period_end",
                "detail": "Use disclosed fiscal period.",
            },
        ],
    )
    assert rows[0]["proposal_decision"] == "REJECTED_POLICY"
    assert rows[0]["canonical_candidate_flag"] == 0
    assert rows[1]["canonical_candidate_flag"] == 0
    assert rows[2]["effective_period_end"] == "2026-02-28"
    assert rows[2]["canonical_candidate_flag"] == 1
    assert summary == {
        "human_review_override_count": 2,
        "human_review_reject_count": 1,
        "human_review_value_fix_count": 0,
        "human_review_period_fix_count": 1,
        "human_review_suppressed_reselection_count": 1,
    }
